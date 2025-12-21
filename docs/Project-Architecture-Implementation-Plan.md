# API-FOOTBALL DATA COLLECTOR - PROJECT ARCHITECTURE & IMPLEMENTATION PLAN

> **Project Type:** Config-Driven Data Pipeline  
> **Tech Stack:** Python 3.11+, PostgreSQL 15+, APScheduler, Redis, Docker  
> **Purpose:** Collect, transform, and serve football data from API-Football v3

---

## Table of Contents

1. [System Architecture Overview](#1-system-architecture-overview)
2. [Core Components](#2-core-components)
3. [Data Flow & Transformations](#3-data-flow--transformations)
4. [Configuration System](#4-configuration-system)
5. [Collector Service Design](#5-collector-service-design)
6. [MCP Layer Design](#6-mcp-layer-design)
7. [Coverage System](#7-coverage-system)
8. [Error Handling & Resilience](#8-error-handling--resilience)
9. [Implementation Roadmap](#9-implementation-roadmap)
10. [Testing Strategy](#10-testing-strategy)

---

## 1. System Architecture Overview

### 1.1. The Big Picture

```
┌─────────────────────────────────────────────────────────────────┐
│                         CONFIG LAYER                             │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │   API    │  │   Job    │  │ Coverage │  │   Rate   │       │
│  │  Config  │  │  Config  │  │  Config  │  │ Limiter  │       │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     COLLECTOR SERVICE                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │ APScheduler  │→ │ Rate Limiter │→ │  API Client  │         │
│  │  (Job Mgr)   │  │ (Token Bucket)│  │(GET+Header) │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                      DATA LAYERS (PostgreSQL)                    │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  RAW: Complete API responses (JSONB archive)             │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ↓ Transform                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  CORE: Normalized business model (UPSERT, FK integrity)  │   │
│  └─────────────────────────────────────────────────────────┘   │
│                          ↓ Aggregate                             │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  MART: Materialized views (Dashboard queries)            │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                         MCP LAYER                                │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │  db_query    │  │coverage_stat │  │rate_limit_st │         │
│  │(Read-only SQL)│  │ (% complete) │  │ (quota left) │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     OPERATIONAL LAYER                            │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐         │
│  │   Logging    │  │ Circuit Br.  │  │   Alerting   │         │
│  │ (Structured) │  │(Error Mgmt)  │  │(Slack/Email) │         │
│  └──────────────┘  └──────────────┘  └──────────────┘         │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2. Design Principles

1. **Config-Driven:** All behavior controlled via configuration files, not code changes
2. **Idempotent:** Can run same operation multiple times safely (UPSERT pattern)
3. **Observable:** Every action logged, metrics tracked, errors alerted
4. **Resilient:** Circuit breakers, retries, fallback to cache
5. **Testable:** Unit tests for transformations, integration tests for jobs
6. **Scalable:** Job priority system, rate limit aware, horizontal scaling ready

---

## 2. Core Components

### 2.1. Component Responsibilities

| Component | Responsibility | Technology |
|-----------|---------------|------------|
| **Config Layer** | Define what to fetch, when, and how | YAML/JSON files |
| **Collector Service** | Execute jobs, enforce rate limits, call API | Python + APScheduler |
| **Rate Limiter** | Token bucket, quota tracking | Python + Redis |
| **API Client** | HTTP GET with `x-apisports-key` header | httpx (async) |
| **Transform Pipeline** | RAW → CORE transformation | Python (Pydantic models) |
| **Database** | Store RAW/CORE/MART data | PostgreSQL 15 |
| **MCP Server** | AI query interface | Python + MCP SDK |
| **Monitoring** | Metrics, logs, alerts | Prometheus + Grafana |

### 2.2. Directory Structure

```
api-football-collector/
├── config/
│   ├── api.yaml              # API connection, base URL, auth
│   ├── jobs/                 # Job definitions
│   │   ├── static.yaml       # Bootstrap jobs (countries, leagues, teams)
│   │   ├── daily.yaml        # Daily jobs (fixtures, standings)
│   │   └── live.yaml         # Live jobs (live fixtures)
│   ├── coverage.yaml         # Coverage targets per league/season
│   └── rate_limiter.yaml     # Quota limits, thresholds
├── src/
│   ├── collector/
│   │   ├── __init__.py
│   │   ├── scheduler.py      # APScheduler setup
│   │   ├── rate_limiter.py   # Token bucket implementation
│   │   ├── api_client.py     # API-Football client
│   │   ├── job_executor.py   # Job execution logic
│   │   └── circuit_breaker.py # Error resilience
│   ├── models/
│   │   ├── raw.py            # RAW layer models
│   │   ├── core.py           # CORE layer models (Pydantic)
│   │   └── mart.py           # MART layer queries
│   ├── transforms/
│   │   ├── fixtures.py       # Fixture transformation
│   │   ├── teams.py          # Team transformation
│   │   ├── players.py        # Player transformation
│   │   └── standings.py      # Standings transformation
│   ├── mcp/
│   │   ├── server.py         # MCP server
│   │   ├── tools.py          # MCP tool definitions
│   │   └── queries.py        # SQL query templates
│   ├── coverage/
│   │   ├── calculator.py     # Coverage % calculation
│   │   └── tracker.py        # Coverage state management
│   └── utils/
│       ├── logging.py        # Structured logging
│       ├── metrics.py        # Prometheus metrics
│       └── alerts.py         # Alert dispatching
├── db/
│   ├── migrations/           # Alembic migrations
│   │   └── versions/
│   ├── schemas/
│   │   ├── raw.sql           # RAW schema DDL
│   │   ├── core.sql          # CORE schema DDL
│   │   └── mart.sql          # MART schema DDL (views)
│   └── seeds/
│       └── initial_config.sql # Initial configuration data
├── tests/
│   ├── unit/
│   │   ├── test_rate_limiter.py
│   │   ├── test_transforms.py
│   │   └── test_coverage.py
│   ├── integration/
│   │   ├── test_jobs.py
│   │   └── test_api_client.py
│   └── fixtures/
│       └── api_responses/    # Mock API responses
├── docker/
│   ├── Dockerfile.collector
│   ├── Dockerfile.mcp
│   └── docker-compose.yml
├── scripts/
│   ├── bootstrap.py          # Initial data load
│   ├── backfill.py           # Historical data backfill
│   └── health_check.py       # Health check endpoint
├── .env.example
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## 3. Data Flow & Transformations

### 3.1. Three-Layer Data Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 1: RAW (Archive)                                          │
│                                                                   │
│  Table: raw.api_responses                                        │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ id (serial), endpoint, params (jsonb), status_code,       │  │
│  │ headers (jsonb), body (jsonb), errors (jsonb),            │  │
│  │ fetched_at                                                 │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                   │
│  Purpose: Complete audit trail, debug API bugs                   │
│  Retention: 90 days (or longer for compliance)                   │
└─────────────────────────────────────────────────────────────────┘
                              ↓ Transform
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 2: CORE (Business Model)                                  │
│                                                                   │
│  Tables: core.fixtures, core.teams, core.players,                │
│          core.leagues, core.standings, core.odds, ...            │
│                                                                   │
│  core.fixtures:                                                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ id (PK), league_id (FK), season, home_team_id (FK),      │  │
│  │ away_team_id (FK), date, status, goals_home, goals_away, │  │
│  │ referee, venue_id (FK), created_at, updated_at           │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                   │
│  core.fixture_details (JSONB for nested data):                   │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ fixture_id (PK, FK), events (jsonb), lineups (jsonb),    │  │
│  │ statistics (jsonb), players (jsonb), updated_at          │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                   │
│  Purpose: Queryable, normalized data with FK integrity           │
│  Retention: Permanent                                             │
└─────────────────────────────────────────────────────────────────┘
                              ↓ Aggregate
┌─────────────────────────────────────────────────────────────────┐
│  LAYER 3: MART (Read Models)                                     │
│                                                                   │
│  Materialized Views:                                              │
│  - mart.daily_fixtures_dashboard                                 │
│  - mart.live_score_panel                                         │
│  - mart.league_summary                                           │
│  - mart.coverage_status                                          │
│                                                                   │
│  Purpose: Pre-aggregated queries for dashboards                  │
│  Refresh: Triggered by CORE updates or scheduled                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2. Transformation Pipeline

**Example: Fixture Transformation**

```python
# Input: RAW API response
raw_response = {
    "get": "fixtures",
    "parameters": {"league": "39", "season": "2024"},
    "errors": [],
    "results": 1,
    "response": [
        {
            "fixture": {
                "id": 1234567,
                "referee": "Michael Oliver",
                "timezone": "UTC",
                "date": "2024-12-12T20:00:00+00:00",
                "timestamp": 1702411200,
                "venue": {"id": 556, "name": "Old Trafford", "city": "Manchester"},
                "status": {"long": "Match Finished", "short": "FT", "elapsed": 90}
            },
            "league": {"id": 39, "name": "Premier League", "season": 2024, ...},
            "teams": {
                "home": {"id": 33, "name": "Manchester United", ...},
                "away": {"id": 34, "name": "Newcastle", ...}
            },
            "goals": {"home": 2, "away": 1},
            "score": {"halftime": {"home": 1, "away": 0}, ...}
        }
    ]
}

# Transform to CORE
from pydantic import BaseModel
from datetime import datetime

class FixtureCore(BaseModel):
    id: int
    league_id: int
    season: int
    home_team_id: int
    away_team_id: int
    date: datetime
    status: str
    goals_home: int | None
    goals_away: int | None
    referee: str | None
    venue_id: int | None

def transform_fixture(raw_data: dict) -> FixtureCore:
    fixture = raw_data["fixture"]
    league = raw_data["league"]
    teams = raw_data["teams"]
    goals = raw_data["goals"]
    
    return FixtureCore(
        id=fixture["id"],
        league_id=league["id"],
        season=league["season"],
        home_team_id=teams["home"]["id"],
        away_team_id=teams["away"]["id"],
        date=datetime.fromisoformat(fixture["date"]),
        status=fixture["status"]["short"],
        goals_home=goals.get("home"),
        goals_away=goals.get("away"),
        referee=fixture.get("referee"),
        venue_id=fixture["venue"]["id"] if fixture.get("venue") else None
    )

# UPSERT to database
def upsert_fixture(conn, fixture: FixtureCore):
    conn.execute("""
        INSERT INTO core.fixtures (
            id, league_id, season, home_team_id, away_team_id,
            date, status, goals_home, goals_away, referee, venue_id, updated_at
        )
        VALUES (
            %(id)s, %(league_id)s, %(season)s, %(home_team_id)s, %(away_team_id)s,
            %(date)s, %(status)s, %(goals_home)s, %(goals_away)s, %(referee)s, %(venue_id)s, NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            status = EXCLUDED.status,
            goals_home = EXCLUDED.goals_home,
            goals_away = EXCLUDED.goals_away,
            updated_at = NOW()
    """, fixture.model_dump())
```

---

## 4. Configuration System

### 4.1. API Configuration (`config/api.yaml`)

```yaml
api:
  base_url: "https://v3.football.api-sports.io"
  api_key_env: "API_FOOTBALL_KEY"  # Environment variable name
  timeout: 30
  default_timezone: "UTC"

rate_limits:
  daily_limit: 7500
  minute_limit: 300
  minute_soft_limit: 250  # Start throttling at this point
  emergency_stop_threshold: 1000  # Stop LOW/MEDIUM jobs

logging:
  level: "INFO"
  format: "json"
  destination: "stdout"
```

### 4.2. Job Configuration (`config/jobs/static.yaml`)

```yaml
jobs:
  - job_id: "bootstrap_countries"
    type: "static_bootstrap"
    enabled: true
    priority: "HIGH"
    endpoint: "/countries"
    params: {}
    interval:
      type: "cron"
      cron: "0 0 1 * *"  # Monthly
    target_layer: "raw+core"
    coverage_target:
      endpoint: "/countries"
      min_count: 100
    
  - job_id: "bootstrap_leagues"
    type: "static_bootstrap"
    enabled: true
    priority: "HIGH"
    endpoint: "/leagues"
    params:
      season: 2024
    interval:
      type: "cron"
      cron: "0 2 * * 0"  # Weekly on Sunday 02:00 UTC
    target_layer: "raw+core"
    dependencies:
      - "bootstrap_countries"
    coverage_target:
      endpoint: "/leagues"
      season: 2024
      min_count: 900
```

### 4.3. Job Configuration (`config/jobs/daily.yaml`)

```yaml
jobs:
  - job_id: "daily_fixtures_pl"
    type: "incremental_daily"
    enabled: true
    priority: "HIGH"
    endpoint: "/fixtures"
    params:
      league: 39
      season: 2024
      date: "{{ today }}"  # Template variable
    interval:
      type: "cron"
      cron: "0 * * * *"  # Hourly
    target_layer: "raw+core+mart"
    dependencies:
      - "bootstrap_leagues"
      - "bootstrap_teams"
    coverage_target:
      endpoint: "/fixtures"
      league_id: 39
      season: 2024
      date_range: "today"
      min_fixtures_count: 10
      max_lag_minutes: 60
```

### 4.4. Job Configuration (`config/jobs/live.yaml`)

```yaml
jobs:
  - job_id: "live_fixtures_all"
    type: "live_loop"
    enabled: true
    priority: "CRITICAL"
    endpoint: "/fixtures"
    params:
      live: "all"
    interval:
      type: "interval"
      seconds: 15
    target_layer: "raw+core+mart"
    filters:
      tracked_leagues: [39, 140, 135, 61, 78]  # Only these leagues
    coverage_target:
      endpoint: "/fixtures"
      live: true
      max_lag_minutes: 1
```

### 4.5. Coverage Configuration (`config/coverage.yaml`)

```yaml
coverage_targets:
  - league_id: 39
    league_name: "Premier League"
    season: 2024
    endpoints:
      - endpoint: "/fixtures"
        min_fixtures_count: 380
        max_lag_minutes: 60
        required_fields: ["status", "goals_home", "goals_away"]
      
      - endpoint: "/standings"
        min_records: 1
        max_lag_minutes: 1440  # 24 hours
      
      - endpoint: "/players"
        min_players: 500
        max_lag_minutes: 10080  # 1 week
    
    alert_thresholds:
      coverage_warning: 95  # %
      coverage_critical: 90  # %
```

---

## 5. Collector Service Design

### 5.1. APScheduler Job Manager

```python
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import yaml

class JobManager:
    def __init__(self, config_path: str):
        self.scheduler = BackgroundScheduler()
        self.jobs = self._load_jobs(config_path)
        self.rate_limiter = RateLimiter()
        self.api_client = APIClient()
    
    def _load_jobs(self, config_path: str) -> list:
        with open(config_path) as f:
            config = yaml.safe_load(f)
        return config["jobs"]
    
    def start(self):
        for job_config in self.jobs:
            if not job_config["enabled"]:
                continue
            
            # Add job to scheduler
            self._schedule_job(job_config)
        
        self.scheduler.start()
    
    def _schedule_job(self, job_config: dict):
        job_id = job_config["job_id"]
        interval = job_config["interval"]
        
        if interval["type"] == "cron":
            trigger = CronTrigger.from_crontab(interval["cron"])
        elif interval["type"] == "interval":
            trigger = IntervalTrigger(seconds=interval["seconds"])
        
        self.scheduler.add_job(
            func=self._execute_job,
            trigger=trigger,
            args=[job_config],
            id=job_id,
            name=job_id,
            replace_existing=True
        )
    
    def _execute_job(self, job_config: dict):
        job_id = job_config["job_id"]
        priority = job_config["priority"]
        
        # Check if job can run (based on quota and priority)
        if not self._can_run_job(priority):
            logger.info(f"Skipping {job_id} - quota too low for priority {priority}")
            return
        
        # Execute job
        try:
            logger.info(f"Starting job {job_id}")
            
            # Acquire rate limiter token
            self.rate_limiter.acquire_token()
            
            # Make API request
            endpoint = job_config["endpoint"]
            params = job_config["params"]
            response = self.api_client.get(endpoint, params)
            
            # Store RAW
            self._store_raw(endpoint, params, response)
            
            # Transform to CORE
            if "core" in job_config["target_layer"]:
                self._transform_to_core(endpoint, response)
            
            # Update MART
            if "mart" in job_config["target_layer"]:
                self._refresh_mart()
            
            # Update coverage
            self._update_coverage(job_config, response)
            
            logger.info(f"Completed job {job_id}")
        
        except Exception as e:
            logger.error(f"Job {job_id} failed: {e}")
            # Circuit breaker logic here
```

### 5.2. Rate Limiter Integration

```python
import time
import redis
from threading import Lock

class RateLimiter:
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.lock = Lock()
        
        # Initialize from config
        self.daily_limit = 7500
        self.minute_limit = 300
        self.minute_soft_limit = 250
        
        # Load state from Redis
        self.daily_remaining = int(self.redis.get("quota:daily:remaining") or self.daily_limit)
        self.minute_remaining = int(self.redis.get("quota:minute:remaining") or self.minute_limit)
    
    def acquire_token(self):
        """Blocking call. Waits until token is available."""
        with self.lock:
            # Check minute quota
            while self.minute_remaining < 1:
                logger.warning("Minute quota exhausted, waiting...")
                time.sleep(1)
                self._refill_minute()
            
            # Check daily quota
            if self.daily_remaining < 1:
                raise Exception("Daily quota exhausted")
            
            # Consume token
            self.minute_remaining -= 1
            self.daily_remaining -= 1
            
            # Persist to Redis
            self.redis.set("quota:minute:remaining", self.minute_remaining)
            self.redis.set("quota:daily:remaining", self.daily_remaining)
    
    def update_from_headers(self, response_headers: dict):
        """Update state from API response headers."""
        with self.lock:
            daily = int(response_headers.get("x-ratelimit-requests-remaining", 0))
            minute = int(response_headers.get("X-RateLimit-Remaining", 0))
            
            # Update if API reports lower values
            if daily < self.daily_remaining:
                self.daily_remaining = daily
                self.redis.set("quota:daily:remaining", daily)
            
            if minute < self.minute_remaining:
                self.minute_remaining = minute
                self.redis.set("quota:minute:remaining", minute)
    
    def _refill_minute(self):
        """Called periodically to refill minute quota."""
        # Simplified: In production, use time-based refill logic
        self.minute_remaining = self.minute_limit
        self.redis.set("quota:minute:remaining", self.minute_limit)
```

### 5.3. API Client

```python
import httpx
from typing import Dict, Any

class APIClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.headers = {"x-apisports-key": api_key}
        self.client = httpx.AsyncClient(timeout=30.0)
    
    async def get(self, endpoint: str, params: Dict[str, Any]) -> Dict:
        """Make GET request to API-Football."""
        url = f"{self.base_url}{endpoint}"
        
        try:
            response = await self.client.get(url, headers=self.headers, params=params)
            
            # Check status
            if response.status_code == 200:
                data = response.json()
                
                # Check errors array
                if data.get("errors"):
                    logger.error(f"API errors: {data['errors']}")
                
                return data
            
            elif response.status_code == 429:
                raise RateLimitError("Rate limit exceeded")
            
            elif response.status_code == 401:
                raise AuthenticationError("Invalid API key")
            
            else:
                raise APIError(f"Unexpected status: {response.status_code}")
        
        except httpx.TimeoutException:
            raise APIError("Request timeout")
```

---

## 6. MCP Layer Design

### 6.1. MCP Server

```python
from mcp.server import Server
from mcp.types import Tool, TextContent

app = Server("api-football-mcp")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="db_query",
            description="Execute read-only SQL query",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "SQL SELECT query"}
                },
                "required": ["query"]
            }
        ),
        Tool(
            name="coverage_status",
            description="Get coverage % for league/season/endpoint",
            inputSchema={
                "type": "object",
                "properties": {
                    "league_id": {"type": "integer"},
                    "season": {"type": "integer"},
                    "endpoint": {"type": "string"}
                },
                "required": ["league_id", "season", "endpoint"]
            }
        ),
        Tool(
            name="rate_limit_status",
            description="Get current quota remaining",
            inputSchema={"type": "object", "properties": {}}
        ),
        Tool(
            name="job_registry",
            description="List all configured jobs",
            inputSchema={"type": "object", "properties": {}}
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "db_query":
        query = arguments["query"]
        # Execute read-only query
        result = await execute_readonly_query(query)
        return [TextContent(type="text", text=str(result))]
    
    elif name == "coverage_status":
        league_id = arguments["league_id"]
        season = arguments["season"]
        endpoint = arguments["endpoint"]
        
        # Calculate coverage
        coverage = await calculate_coverage(league_id, season, endpoint)
        return [TextContent(type="text", text=f"Coverage: {coverage}%")]
    
    elif name == "rate_limit_status":
        daily = redis.get("quota:daily:remaining")
        minute = redis.get("quota:minute:remaining")
        return [TextContent(
            type="text",
            text=f"Daily: {daily}/7500, Minute: {minute}/300"
        )]
    
    elif name == "job_registry":
        jobs = load_all_jobs()
        return [TextContent(type="text", text=yaml.dump(jobs))]
```

### 6.2. Pre-defined Query Templates

```python
QUERY_TEMPLATES = {
    "raw_vs_core_fixtures": """
        SELECT 
            'RAW' as layer, COUNT(*) as count
        FROM raw.api_responses
        WHERE endpoint = '/fixtures' AND fetched_at > NOW() - INTERVAL '24 hours'
        UNION ALL
        SELECT 
            'CORE' as layer, COUNT(*) as count
        FROM core.fixtures
        WHERE updated_at > NOW() - INTERVAL '24 hours'
    """,
    
    "coverage_by_league": """
        SELECT 
            l.id as league_id,
            l.name as league_name,
            COUNT(DISTINCT f.id) as fixture_count,
            c.min_fixtures_count as expected_count,
            ROUND(100.0 * COUNT(DISTINCT f.id) / c.min_fixtures_count, 2) as coverage_pct
        FROM core.leagues l
        LEFT JOIN core.fixtures f ON f.league_id = l.id AND f.season = %(season)s
        LEFT JOIN coverage_targets c ON c.league_id = l.id AND c.season = %(season)s
        GROUP BY l.id, l.name, c.min_fixtures_count
        ORDER BY coverage_pct ASC
    """,
    
    "live_fixtures_status": """
        SELECT 
            status,
            COUNT(*) as count
        FROM core.fixtures
        WHERE status IN ('1H', '2H', 'HT', 'ET', 'BT', 'P', 'SUSP', 'INT')
        AND updated_at > NOW() - INTERVAL '2 minutes'
        GROUP BY status
    """
}
```

---

## 7. Coverage System

### 7.1. Coverage Calculator

```python
class CoverageCalculator:
    def __init__(self, db_conn):
        self.conn = db_conn
    
    def calculate(self, league_id: int, season: int, endpoint: str) -> dict:
        """Calculate coverage metrics."""
        
        if endpoint == "/fixtures":
            return self._calculate_fixtures_coverage(league_id, season)
        elif endpoint == "/standings":
            return self._calculate_standings_coverage(league_id, season)
        # ... other endpoints
    
    def _calculate_fixtures_coverage(self, league_id: int, season: int) -> dict:
        # Get expected count from config
        target = self._get_coverage_target(league_id, season, "/fixtures")
        expected_count = target["min_fixtures_count"]
        
        # Get actual count from CORE
        actual_count = self.conn.execute("""
            SELECT COUNT(*) FROM core.fixtures
            WHERE league_id = %s AND season = %s
        """, (league_id, season)).fetchone()[0]
        
        # Calculate count coverage
        count_coverage = (actual_count / expected_count) * 100 if expected_count > 0 else 0
        
        # Calculate freshness coverage
        last_update = self.conn.execute("""
            SELECT MAX(updated_at) FROM core.fixtures
            WHERE league_id = %s AND season = %s
        """, (league_id, season)).fetchone()[0]
        
        lag_minutes = (datetime.now(timezone.utc) - last_update).total_seconds() / 60
        max_lag = target.get("max_lag_minutes", 60)
        freshness_coverage = max(0, 100 - (lag_minutes / max_lag) * 100)
        
        # Calculate pipeline coverage (RAW → CORE ratio)
        raw_count = self.conn.execute("""
            SELECT COUNT(*) FROM raw.api_responses
            WHERE endpoint = '/fixtures'
            AND params->>'league' = %s
            AND params->>'season' = %s
            AND fetched_at > NOW() - INTERVAL '24 hours'
        """, (str(league_id), str(season))).fetchone()[0]
        
        pipeline_coverage = (actual_count / raw_count) * 100 if raw_count > 0 else 100
        
        # Overall coverage (weighted average)
        overall = (
            count_coverage * 0.5 +
            freshness_coverage * 0.3 +
            pipeline_coverage * 0.2
        )
        
        return {
            "league_id": league_id,
            "season": season,
            "endpoint": "/fixtures",
            "count_coverage": round(count_coverage, 2),
            "freshness_coverage": round(freshness_coverage, 2),
            "pipeline_coverage": round(pipeline_coverage, 2),
            "overall_coverage": round(overall, 2),
            "actual_count": actual_count,
            "expected_count": expected_count,
            "lag_minutes": round(lag_minutes, 2)
        }
```

---

## 8. Error Handling & Resilience

### 8.1. Circuit Breaker per Endpoint

```python
from collections import defaultdict
import time

class CircuitBreakerManager:
    def __init__(self):
        self.breakers = defaultdict(lambda: CircuitBreaker())
    
    def execute(self, endpoint: str, func, *args, **kwargs):
        breaker = self.breakers[endpoint]
        return breaker.call(func, *args, **kwargs)

class CircuitBreaker:
    def __init__(self, failure_threshold=5, timeout=60):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.state = 'CLOSED'
        self.next_attempt = None
    
    def call(self, func, *args, **kwargs):
        if self.state == 'OPEN':
            if time.time() < self.next_attempt:
                raise CircuitOpenError(f"Circuit open until {self.next_attempt}")
            self.state = 'HALF_OPEN'
        
        try:
            result = func(*args, **kwargs)
            self.on_success()
            return result
        except Exception as e:
            self.on_failure()
            raise
    
    def on_success(self):
        self.failure_count = 0
        self.state = 'CLOSED'
    
    def on_failure(self):
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = 'OPEN'
            self.next_attempt = time.time() + self.timeout
            logger.critical(f"🔴 Circuit breaker OPEN for {self.timeout}s")
```

---

## 9. Implementation Roadmap

### Phase 1: Foundation (Week 1-2)

**Goals:**
- Set up project structure
- Implement configuration system
- Create database schemas (RAW/CORE/MART)
- Basic API client

**Deliverables:**
- [ ] Project directory structure
- [ ] `config/` files (api.yaml, jobs/*.yaml, coverage.yaml)
- [ ] PostgreSQL schemas (raw.sql, core.sql, mart.sql)
- [ ] Alembic migrations setup
- [ ] API client with rate limiter (Token Bucket)
- [ ] Docker Compose for local development

**Validation:**
- [ ] Can call `/status` endpoint successfully
- [ ] Rate limiter blocks when tokens exhausted
- [ ] Database schemas created without errors

### Phase 2: Static Data Bootstrap (Week 3)

**Goals:**
- Implement bootstrap jobs for static data
- Transform RAW → CORE for countries, leagues, teams

**Deliverables:**
- [ ] Job: `bootstrap_countries`
- [ ] Job: `bootstrap_leagues`
- [ ] Job: `bootstrap_teams`
- [ ] Transform: `countries.py`, `leagues.py`, `teams.py`
- [ ] UPSERT logic for all core tables

**Validation:**
- [ ] `core.countries` populated with 200+ countries
- [ ] `core.leagues` populated with 900+ leagues
- [ ] `core.teams` populated for tracked leagues
- [ ] All FK constraints satisfied

### Phase 3: Daily & Live Jobs (Week 4-5)

**Goals:**
- Implement daily fixture sync
- Implement live score monitoring
- Coverage tracking

**Deliverables:**
- [ ] Job: `daily_fixtures` (per league)
- [ ] Job: `live_fixtures_all`
- [ ] Transform: `fixtures.py`
- [ ] Delta detection logic
- [ ] Coverage calculator

**Validation:**
- [ ] Daily fixtures updated hourly
- [ ] Live scores update every 15 seconds
- [ ] Coverage % calculated correctly
- [ ] Only changed fixtures trigger DB writes

### Phase 4: MCP Integration (Week 6)

**Goals:**
- MCP server for AI queries
- Pre-defined query templates

**Deliverables:**
- [ ] MCP server (`mcp/server.py`)
- [ ] Tools: `db_query`, `coverage_status`, `rate_limit_status`, `job_registry`
- [ ] Query templates for common questions

**Validation:**
- [ ] AI can query coverage status
- [ ] AI can check rate limit
- [ ] AI can inspect job registry

### Phase 5: Error Handling & Monitoring (Week 7)

**Goals:**
- Circuit breakers
- Alerting system
- Prometheus metrics

**Deliverables:**
- [ ] Circuit breaker per endpoint
- [ ] Alert dispatcher (Slack/Email)
- [ ] Prometheus exporter
- [ ] Grafana dashboards

**Validation:**
- [ ] Circuit breaker opens after 5 failures
- [ ] Alerts sent when quota < 1000
- [ ] Metrics visible in Grafana

### Phase 6: Production Deployment (Week 8)

**Goals:**
- Production-ready deployment
- Backup strategy
- Documentation

**Deliverables:**
- [ ] Production Docker Compose
- [ ] PostgreSQL backup automation
- [ ] Health check endpoint
- [ ] README with setup instructions

**Validation:**
- [ ] System runs for 24 hours without crashes
- [ ] All jobs execute on schedule
- [ ] Coverage > 95% for all tracked leagues

---

## 10. Testing Strategy

### 10.1. Unit Tests

```python
# tests/unit/test_rate_limiter.py
def test_rate_limiter_blocks_when_exhausted():
    limiter = RateLimiter(max_tokens=2, refill_rate=1.0)
    
    limiter.acquire_token()  # Token 1
    limiter.acquire_token()  # Token 2
    
    # Should block on third call
    start = time.time()
    limiter.acquire_token()  # Token 3 (waits 1 second)
    elapsed = time.time() - start
    
    assert elapsed >= 0.9  # Approximately 1 second wait

# tests/unit/test_transforms.py
def test_fixture_transform():
    raw_response = load_fixture("api_responses/fixtures_response.json")
    fixture = transform_fixture(raw_response["response"][0])
    
    assert fixture.id == 1234567
    assert fixture.league_id == 39
    assert fixture.status == "FT"
    assert fixture.goals_home == 2
    assert fixture.goals_away == 1
```

### 10.2. Integration Tests

```python
# tests/integration/test_jobs.py
@pytest.mark.asyncio
async def test_bootstrap_leagues_job(test_db):
    job_config = {
        "job_id": "test_bootstrap_leagues",
        "endpoint": "/leagues",
        "params": {"season": 2024},
        "target_layer": "raw+core"
    }
    
    # Execute job
    await execute_job(job_config)
    
    # Verify RAW
    raw_count = test_db.execute(
        "SELECT COUNT(*) FROM raw.api_responses WHERE endpoint='/leagues'"
    ).fetchone()[0]
    assert raw_count > 0
    
    # Verify CORE
    core_count = test_db.execute(
        "SELECT COUNT(*) FROM core.leagues WHERE season=2024"
    ).fetchone()[0]
    assert core_count > 0
```

### 10.3. Coverage Tests

```python
# tests/unit/test_coverage.py
def test_coverage_calculation():
    calculator = CoverageCalculator(mock_db)
    
    # Mock data: 38 fixtures out of 40 expected
    result = calculator.calculate(league_id=39, season=2024, endpoint="/fixtures")
    
    assert result["actual_count"] == 38
    assert result["expected_count"] == 40
    assert result["count_coverage"] == 95.0
```

---

## Appendix: Key Decisions

### Decision 1: Why Config-Driven?
**Problem:** Hard-coded league IDs in code require deployment to add/remove leagues.  
**Solution:** All leagues, jobs, and thresholds in YAML config files.  
**Benefit:** Add new league by editing config, no code change.

### Decision 2: Why Three Layers (RAW/CORE/MART)?
**Problem:** API responses change, need audit trail and fast queries.  
**Solution:** RAW (archive), CORE (business model), MART (read models).  
**Benefit:** Debug API bugs, ensure data quality, optimize queries.

### Decision 3: Why JSONB for Nested Data?
**Problem:** API responses deeply nested (events, lineups, statistics).  
**Solution:** Store in JSONB columns instead of 50+ relational tables.  
**Benefit:** Low transform cost, serve JSON directly to frontend.

### Decision 4: Why APScheduler over Cron?
**Problem:** Need sub-minute intervals (15 seconds for live), dynamic job management.  
**Solution:** APScheduler with IntervalTrigger.  
**Benefit:** Dynamic job add/remove, sub-minute precision, Python integration.

### Decision 5: Why MCP Layer?
**Problem:** AI code editor needs to query DB, check coverage, inspect jobs.  
**Solution:** MCP server with read-only tools.  
**Benefit:** AI can debug, suggest optimizations, monitor system health.

---

**Document Version:** 1.0  
**Last Updated:** 2024-12-12  
**Status:** Production-Ready Design
