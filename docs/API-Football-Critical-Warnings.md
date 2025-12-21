# API-FOOTBALL COLLECTOR - CRITICAL WARNINGS & RED LINES

> **Target Plan:** Pro Plan (7,500 requests/day, ~300 requests/minute)  
> **Document Purpose:** Prevent quota exhaustion, data corruption, and production failures  
> **Severity Levels:** 🔴 CRITICAL | ⚠️ WARNING | ℹ️ INFO

---

## Table of Contents

1. [Rate Limiting & Quota Management](#1-rate-limiting--quota-management)
2. [Data Architecture & Database Design](#2-data-architecture--database-design)
3. [Time Management](#3-time-management)
4. [Live Data Flow (Live Score)](#4-live-data-flow-live-score)
5. [Odds Management](#5-odds-management)
6. [Security & Error Handling](#6-security--error-handling)
7. [Production Deployment](#7-production-deployment)
8. [Verification Checklist](#8-verification-checklist)

---

## 1. Rate Limiting & Quota Management

### 1.1. The Quota Reality Check

**Pro Plan Limits:**
- 🔴 **Daily:** 7,500 requests
- 🔴 **Per Minute:** ~300 requests (community-reported, not official)
- 🔴 **Exceeding:** Automatic firewall ban (no warning, no extra charge)

**Reality Check:**
- 50 leagues × 10 requests (fixtures + standings + stats) = **500 requests** (just for static data)
- If you're tracking 100+ leagues with live updates, you'll exhaust quota in hours without optimization

### 1.2. 🔴 CRITICAL: Client-Side Rate Limiter is MANDATORY

**Never rely on API rate limiting.** By the time API rejects your request, you've already wasted quota and risk firewall ban.

#### Token Bucket Implementation (Python)

```python
import time
from threading import Lock

class RateLimiter:
    def __init__(self, max_tokens=300, refill_rate=5.0):
        """
        max_tokens: Per-minute limit (300)
        refill_rate: Tokens per second (300/60 = 5)
        """
        self.max_tokens = max_tokens
        self.tokens = max_tokens
        self.refill_rate = refill_rate
        self.last_refill = time.time()
        self.lock = Lock()
    
    def acquire_token(self):
        """Blocking call. Waits until token is available."""
        with self.lock:
            self._refill()
            
            while self.tokens < 1:
                wait_time = (1 / self.refill_rate)
                time.sleep(wait_time)
                self._refill()
            
            self.tokens -= 1
    
    def _refill(self):
        now = time.time()
        time_passed = now - self.last_refill
        tokens_to_add = time_passed * self.refill_rate
        
        self.tokens = min(self.max_tokens, self.tokens + tokens_to_add)
        self.last_refill = now
    
    def update_from_headers(self, daily_remaining, minute_remaining):
        """Update state from API response headers."""
        with self.lock:
            # Adjust tokens based on actual API state
            if minute_remaining < self.tokens:
                self.tokens = minute_remaining

# Usage
limiter = RateLimiter(max_tokens=300, refill_rate=5)

def make_api_request(endpoint, params):
    limiter.acquire_token()  # Blocks until token available
    response = requests.get(f"{BASE_URL}{endpoint}", headers=HEADERS, params=params)
    
    # Update limiter from response headers
    daily_remaining = int(response.headers.get('x-ratelimit-requests-remaining', 0))
    minute_remaining = int(response.headers.get('X-RateLimit-Remaining', 0))
    limiter.update_from_headers(daily_remaining, minute_remaining)
    
    return response
```

### 1.3. Response Header Tracking

**🔴 CRITICAL:** Read these headers on EVERY response:

```python
def track_quota(response):
    daily_limit = response.headers.get('x-ratelimit-requests-limit')
    daily_remaining = response.headers.get('x-ratelimit-requests-remaining')
    minute_limit = response.headers.get('X-RateLimit-Limit')
    minute_remaining = response.headers.get('X-RateLimit-Remaining')
    
    print(f"Daily: {daily_remaining}/{daily_limit}")
    print(f"Minute: {minute_remaining}/{minute_limit}")
    
    # ⚠️ WARNING: Low quota
    if int(daily_remaining) < 1000:
        logger.warning(f"⚠️ Daily quota below 1000! Remaining: {daily_remaining}")
        # Enter "low power mode" - disable non-critical jobs
    
    # 🔴 CRITICAL: Very low quota
    if int(daily_remaining) < 500:
        logger.critical(f"🔴 Daily quota below 500! Remaining: {daily_remaining}")
        # Stop ALL non-essential jobs
```

### 1.4. Quota Reset Schedule

**Daily quota resets at 00:00 UTC.**

**Best Practices:**
- ✅ Schedule heavy operations (backfill, bulk sync) at 00:30 UTC
- ✅ Use UTC-based cron jobs
- ✅ Plan daily sync operations around reset time

**Example Cron Schedule:**
```bash
# Daily full sync - 00:30 UTC (30 minutes after quota reset)
30 0 * * * /usr/bin/python /app/scripts/daily_sync.py

# Standings update - 01:00 UTC
0 1 * * * /usr/bin/python /app/scripts/update_standings.py

# Live score monitoring - Every 15 seconds (during match hours)
# Use APScheduler instead of cron for sub-minute intervals
```

### 1.5. Job Priority System

When quota is low, implement priority-based job execution:

**Priority Levels:**
1. 🔴 **CRITICAL** (Always run): Live fixtures (`/fixtures?live=all`), today's matches
2. ⚠️ **HIGH** (Run if quota > 1000): Standings, team statistics
3. ℹ️ **MEDIUM** (Run if quota > 3000): Player statistics, injuries
4. 📦 **LOW** (Run if quota > 5000): Historical data, backfill operations

**Implementation:**
```python
class JobManager:
    def __init__(self, rate_limiter):
        self.rate_limiter = rate_limiter
        self.daily_remaining = 7500
    
    def can_run_job(self, job_priority):
        if job_priority == "CRITICAL":
            return self.daily_remaining > 100  # Always run unless quota exhausted
        elif job_priority == "HIGH":
            return self.daily_remaining > 1000
        elif job_priority == "MEDIUM":
            return self.daily_remaining > 3000
        elif job_priority == "LOW":
            return self.daily_remaining > 5000
        return False
    
    def execute_job(self, job):
        if not self.can_run_job(job.priority):
            logger.info(f"Skipping {job.name} - quota too low")
            return
        
        # Execute job...
        self.rate_limiter.acquire_token()
        # ... make API call ...
```

---

## 2. Data Architecture & Database Design

### 2.1. Entity IDs are IMMUTABLE

**✅ These IDs NEVER change:**
- `fixture_id` - Even if match is postponed, cancelled, or rescheduled
- `team_id` - Even if team changes league or name
- `player_id` - Even if player transfers to another team
- `league_id` - Even across seasons

**🔴 CRITICAL: Use these IDs as PRIMARY KEYS in your database.**

### 2.2. UPSERT is MANDATORY

**❌ NEVER use plain INSERT for API data.**

**Why?** API data changes (live score updates, status changes). Plain INSERT creates duplicates.

**✅ Always use UPSERT (INSERT ... ON CONFLICT):**

```sql
-- PostgreSQL Example
INSERT INTO core.fixtures (
    id, league_id, season, home_team_id, away_team_id,
    date, status, goals_home, goals_away, updated_at
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
ON CONFLICT (id) DO UPDATE SET
    status = EXCLUDED.status,
    goals_home = EXCLUDED.goals_home,
    goals_away = EXCLUDED.goals_away,
    updated_at = NOW();
```

**Benefits:**
- ✅ Idempotent (can run same operation multiple times safely)
- ✅ No duplicates
- ✅ Live score updates work correctly (same fixture ID, different score)
- ✅ Status transitions work (NS → 1H → HT → 2H → FT)

### 2.3. Schema Design: Hybrid Approach (Relational + JSONB)

API responses are deeply nested JSON. **Don't try to normalize everything.**

**Recommended Approach:**

#### Core Tables (Relational)
Store searchable, frequently queried data:

```sql
CREATE TABLE core.fixtures (
    id BIGINT PRIMARY KEY,
    league_id INT NOT NULL,
    season INT NOT NULL,
    home_team_id INT NOT NULL,
    away_team_id INT NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    status VARCHAR(20),
    goals_home INT,
    goals_away INT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_fixtures_date ON core.fixtures(date);
CREATE INDEX idx_fixtures_status ON core.fixtures(status);
CREATE INDEX idx_fixtures_league_season ON core.fixtures(league_id, season);
```

#### Detail Tables (JSONB)
Store complex, variable data as JSON:

```sql
CREATE TABLE core.fixture_details (
    fixture_id BIGINT PRIMARY KEY REFERENCES core.fixtures(id),
    events JSONB,        -- Goals, cards, substitutions
    lineups JSONB,       -- Starting XI, formations
    statistics JSONB,    -- Shots, possession, corners
    players JSONB,       -- Player-level stats
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- JSONB indexes for efficient queries
CREATE INDEX idx_fixture_details_events ON core.fixture_details USING GIN (events);
```

**Why JSONB?**
- ✅ Low parsing cost (store API response almost as-is)
- ✅ Serve directly to frontend (no transformation)
- ✅ Fast relational queries on core table
- ✅ Flexible queries on JSONB (e.g., "find all goals by player X")

### 2.4. RAW / CORE / MART Architecture

**Three-layer architecture for data quality and debugging:**

#### Layer 1: RAW (Archive)
```sql
CREATE TABLE raw.api_responses (
    id SERIAL PRIMARY KEY,
    endpoint VARCHAR(255) NOT NULL,
    params JSONB,
    status_code INT,
    headers JSONB,
    body JSONB,          -- Complete API response
    errors JSONB,        -- API errors array
    fetched_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_raw_endpoint ON raw.api_responses(endpoint, fetched_at);
```

**Purpose:**
- ✅ Complete audit trail
- ✅ Debug API bugs or data anomalies
- ✅ Replay/reprocess data if needed
- ✅ Compliance (data retention requirements)

#### Layer 2: CORE (Business Model)
```sql
-- Normalized, relational tables
-- Examples: core.fixtures, core.teams, core.players, core.standings
```

**Purpose:**
- ✅ Clean, queryable business model
- ✅ Foreign key integrity
- ✅ Optimized for application queries

#### Layer 3: MART (Analytics/Reports)
```sql
CREATE MATERIALIZED VIEW mart.daily_fixtures_dashboard AS
SELECT 
    f.date::date,
    f.league_id,
    l.name as league_name,
    COUNT(*) as total_fixtures,
    SUM(CASE WHEN f.status = 'FT' THEN 1 ELSE 0 END) as completed,
    SUM(CASE WHEN f.status IN ('1H', '2H', 'HT') THEN 1 ELSE 0 END) as live
FROM core.fixtures f
JOIN core.leagues l ON f.league_id = l.id
GROUP BY f.date::date, f.league_id, l.name;

CREATE INDEX idx_mart_fixtures_date ON mart.daily_fixtures_dashboard(date);
```

**Purpose:**
- ✅ Pre-aggregated reports
- ✅ Fast dashboard queries
- ✅ Historical snapshots

### 2.5. 🔴 CRITICAL: Foreign Key Dependencies

**NEVER try to insert data out of order.**

**Correct Order:**
```
1. Countries → Timezones (static)
2. Leagues (with season)
3. Teams (with league + season)
4. Venues (often comes with teams)
5. Fixtures (requires league_id, home_team_id, away_team_id)
6. Players (requires team_id)
7. Odds (requires fixture_id)
```

**Wrong Order Example (will fail):**
```python
# ❌ WRONG - Tries to insert fixture before teams exist
fetch_and_store("/fixtures?league=39&season=2024")  # FAILS with FK error
fetch_and_store("/teams?league=39&season=2024")      # Too late

# ✅ CORRECT
fetch_and_store("/teams?league=39&season=2024")      # Teams first
fetch_and_store("/fixtures?league=39&season=2024")   # Then fixtures
```

### 2.6. Media Assets (Logos, Images)

**🔴 CRITICAL: Do NOT hotlink API media URLs.**

API media URLs (`media.api-sports.io`) are:
- ✅ Free (don't count toward quota)
- ❌ Rate-limited per second/minute
- ❌ Not guaranteed for hotlinking

**Recommended Approach:**

1. **First Request:** Fetch image from API
2. **Optimize:** Convert to WebP, resize as needed
3. **Store:** Upload to your object storage (AWS S3, Cloudflare R2, etc.)
4. **Serve:** Deliver via your CDN

**Example:**
```python
import httpx
from PIL import Image
from io import BytesIO

async def cache_team_logo(team_id, api_logo_url):
    # Check if already cached
    cached_url = await redis.get(f"logo:{team_id}")
    if cached_url:
        return cached_url
    
    # Fetch from API
    async with httpx.AsyncClient() as client:
        response = await client.get(api_logo_url)
        img_bytes = response.content
    
    # Optimize
    img = Image.open(BytesIO(img_bytes))
    img = img.resize((200, 200), Image.LANCZOS)
    
    # Convert to WebP
    webp_buffer = BytesIO()
    img.save(webp_buffer, format='WEBP', quality=85)
    
    # Upload to S3/R2
    cdn_url = await upload_to_s3(f"logos/{team_id}.webp", webp_buffer.getvalue())
    
    # Cache URL for 30 days
    await redis.set(f"logo:{team_id}", cdn_url, ex=86400*30)
    
    return cdn_url
```

---

## 3. Time Management

### 3.1. 🔴 CRITICAL: Always Store UTC

**NEVER store timestamps in local timezone.**

**Why?**
- Daylight Saving Time (DST) breaks data integrity
- Server migration issues
- Multi-timezone user issues
- Query inconsistencies

**✅ CORRECT:**
```python
from datetime import datetime, timezone

# Store UTC
fixture_date = datetime.now(timezone.utc)

# PostgreSQL
CREATE TABLE fixtures (
    date TIMESTAMPTZ NOT NULL  -- TIMESTAMPTZ stores with timezone
);

# Python
import psycopg2
cursor.execute(
    "INSERT INTO fixtures (id, date) VALUES (%s, %s)",
    (fixture_id, datetime.now(timezone.utc))
)
```

**❌ WRONG:**
```python
# Local time (affected by DST)
fixture_date = datetime.now()  # NO TIMEZONE

# Naive timestamp
CREATE TABLE fixtures (
    date TIMESTAMP NOT NULL  -- No timezone info
);
```

### 3.2. Display Timezone Conversion (Client-Side)

**Store UTC, convert on display:**

```python
# Backend (store UTC)
fixture = {
    "id": 1234567,
    "date": "2024-12-12T20:00:00+00:00"  # UTC
}

# Frontend (convert to user timezone)
const fixtureDate = new Date("2024-12-12T20:00:00+00:00");
const userTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const localTime = fixtureDate.toLocaleString('en-US', { 
    timeZone: userTimezone 
});
```

### 3.3. API Timezone Parameter

**API supports `timezone` parameter for display:**

```bash
# Returns times in Europe/Istanbul timezone
GET /fixtures?date=2024-12-12&league=39&timezone=Europe/Istanbul
```

**⚠️ WARNING:** Only use `timezone` parameter for user-facing display. ALWAYS store UTC in database.

---

## 4. Live Data Flow (Live Score)

### 4.1. 🔴 CRITICAL: No WebSocket Support

**API-Football does NOT provide WebSocket.** You must use HTTP polling.

### 4.2. ❌ WRONG: Per-Fixture Polling

**This exhausts quota rapidly:**

```python
# ❌ BAD - 50 live fixtures = 50 requests every 15 seconds
live_fixtures = [1234567, 1234568, 1234569, ...]  # 50 IDs

for fixture_id in live_fixtures:
    response = requests.get(f"/fixtures?id={fixture_id}")
    # Process...

# Result: 50 requests × 4 per minute = 200 requests/minute
# Just live updates consume 66% of your per-minute quota!
```

### 4.3. ✅ CORRECT: Single `live=all` Request

**This is efficient:**

```python
# ✅ GOOD - ALL live fixtures in 1 request
response = requests.get("/fixtures?live=all")
all_live_fixtures = response.json()["response"]

# Filter to tracked leagues
tracked_league_ids = {39, 140, 135, 61, 78}  # From config
relevant_fixtures = [
    f for f in all_live_fixtures 
    if f["league"]["id"] in tracked_league_ids
]

# Process only relevant fixtures
for fixture in relevant_fixtures:
    # Delta detection (only update if changed)
    if has_changed(fixture):
        update_database(fixture)

# Result: 1 request per 15 seconds = 4 requests/minute
# Only 1.3% of per-minute quota!
```

### 4.4. Delta Detection Pattern

**Don't blindly update database on every poll. Use delta detection:**

```python
import redis
import json

redis_client = redis.Redis()

def has_changed(fixture):
    fixture_id = fixture["fixture"]["id"]
    cache_key = f"fixture:{fixture_id}"
    
    # Get cached state
    cached = redis_client.get(cache_key)
    if not cached:
        return True
    
    cached_data = json.loads(cached)
    
    # Check if critical fields changed
    changed = (
        cached_data["goals"]["home"] != fixture["goals"]["home"] or
        cached_data["goals"]["away"] != fixture["goals"]["away"] or
        cached_data["fixture"]["status"]["short"] != fixture["fixture"]["status"]["short"] or
        cached_data["fixture"]["status"]["elapsed"] != fixture["fixture"]["status"]["elapsed"]
    )
    
    return changed

def update_database(fixture):
    fixture_id = fixture["fixture"]["id"]
    
    # UPSERT to database
    cursor.execute("""
        INSERT INTO core.fixtures (id, status, goals_home, goals_away, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE SET
            status = EXCLUDED.status,
            goals_home = EXCLUDED.goals_home,
            goals_away = EXCLUDED.goals_away,
            updated_at = NOW()
    """, (
        fixture_id,
        fixture["fixture"]["status"]["short"],
        fixture["goals"]["home"],
        fixture["goals"]["away"]
    ))
    
    # Update cache (60 second expiry)
    redis_client.set(
        f"fixture:{fixture_id}",
        json.dumps(fixture),
        ex=60
    )
    
    # Push to connected clients via WebSocket
    websocket_broadcast(f"fixture:{fixture_id}", fixture)
```

### 4.5. Live Polling Schedule

**Match Status-Based Polling:**

| Status | Polling Frequency | Reason |
|--------|------------------|--------|
| TBD, PST | Once per day | Date not confirmed |
| NS (< 24h) | Every 6 hours | Check for lineup updates |
| NS (< 1h) | Every 15 minutes | Lineups available 1h before |
| 1H, 2H, HT, ET | Every 15-20 seconds | Live match |
| FT, AET, PEN | Final fetch, then stop | Match completed |
| CANC, ABD | Final fetch, then stop | Match cancelled/abandoned |

**Implementation:**
```python
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()

def live_score_loop():
    response = requests.get("/fixtures?live=all")
    # Process as shown above...

# Run every 15 seconds
scheduler.add_job(live_score_loop, 'interval', seconds=15)
scheduler.start()
```

### 4.6. Detailed Statistics (On-Demand)

**Live fixtures include basic data (score, status). Detailed stats require separate calls.**

**Strategy:**
- User navigates to match detail page → Fetch `/fixtures/statistics?fixture={id}`
- Cache for 1 minute to prevent duplicate requests

```python
@lru_cache(maxsize=100)
def get_fixture_statistics(fixture_id, cache_time=60):
    """Cached for 60 seconds."""
    response = requests.get(f"/fixtures/statistics?fixture={fixture_id}")
    return response.json()
```

---

## 5. Odds Management

### 5.1. ⚠️ WARNING: Data Retention Policy Unconfirmed

**Community reports suggest odds data is retained for 3 months.** This is NOT officially documented.

**Recommendations:**
1. ✅ Verify retention policy with API-Football support
2. ✅ Archive critical odds data in your database
3. ✅ Fetch and store odds immediately after match completion

### 5.2. Pagination with Odds

**Odds endpoints return paginated data (can be large):**

```python
async def fetch_all_odds(fixture_id):
    all_odds = []
    page = 1
    has_more = True
    
    while has_more:
        # Check quota before each request
        if daily_remaining < 100:
            logger.warning("Quota low, stopping odds fetch")
            break
        
        await rate_limiter.acquire_token()
        
        response = requests.get(f"/odds?fixture={fixture_id}&page={page}")
        data = response.json()
        
        all_odds.extend(data["response"])
        
        # Check pagination
        paging = data["paging"]
        has_more = paging["current"] < paging["total"]
        page += 1
    
    return all_odds
```

### 5.3. 🔴 CRITICAL: Never Query `/odds` Without Filters

**❌ WRONG:**
```python
# This returns massive paginated data, exhausts quota
response = requests.get("/odds")
```

**✅ CORRECT:**
```python
# Always filter by league, season, or date
response = requests.get("/odds?league=39&season=2024&date=2024-12-12")
```

---

## 6. Security & Error Handling

### 6.1. 🔴 CRITICAL: API Key Security

**NEVER do these:**
- ❌ Put API key in frontend code (JavaScript/React/Vue)
- ❌ Embed API key in mobile app (APK/IPA can be decompiled)
- ❌ Commit API key to Git repository
- ❌ Allow clients to call API-Football directly

**✅ CORRECT: Backend Proxy Pattern**

```
[Client (Web/Mobile)]
    ↓ (No API key)
[Your Backend API Gateway]
    ↓ (Add API key)
[API-Football]
```

**Express.js Proxy Example:**
```javascript
// .env file (NEVER commit to Git)
API_FOOTBALL_KEY=your_secret_key_here

// server.js
require('dotenv').config();

app.get('/api/fixtures', async (req, res) => {
    // Validate client request
    const { league, season } = req.query;
    if (!league || !season) {
        return res.status(400).json({ error: 'League and season required' });
    }
    
    // Rate limiting
    await rateLimiter.acquireToken();
    
    // Proxy to API-Football (API key added here)
    const response = await fetch(
        `https://v3.football.api-sports.io/fixtures?league=${league}&season=${season}`,
        {
            headers: {
                'x-apisports-key': process.env.API_FOOTBALL_KEY
            }
        }
    );
    
    const data = await response.json();
    res.json(data);
});
```

### 6.2. Circuit Breaker Pattern

**Prevent repeated failures from exhausting quota:**

```python
class CircuitBreaker:
    def __init__(self, failure_threshold=5, timeout=60):
        self.failure_count = 0
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.state = 'CLOSED'  # CLOSED, OPEN, HALF_OPEN
        self.next_attempt = None
    
    def call(self, func, *args, **kwargs):
        if self.state == 'OPEN':
            if time.time() < self.next_attempt:
                raise Exception("Circuit breaker is OPEN")
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

# Usage
breaker = CircuitBreaker(failure_threshold=5, timeout=60)

def fetch_fixtures():
    try:
        return breaker.call(lambda: requests.get("/fixtures"))
    except Exception as e:
        # Circuit open, return cached data
        return get_cached_fixtures()
```

### 6.3. HTTP Status Code Handling

```python
def handle_api_response(response):
    status = response.status_code
    
    if status == 200:
        # Check errors array even on 200
        data = response.json()
        if data.get("errors"):
            logger.error(f"API error: {data['errors']}")
        return data
    
    elif status == 204:
        # No content (valid query, no results)
        logger.info("No results found")
        return None
    
    elif status == 401:
        # 🔴 CRITICAL: Invalid API key
        logger.critical("🔴 Invalid API key!")
        send_alert("API key invalid or expired")
        raise Exception("Authentication failed")
    
    elif status == 429:
        # Rate limit exceeded
        logger.error("Rate limit exceeded")
        # Exponential backoff
        time.sleep(2 ** retry_count)
        return None
    
    elif status in (500, 502, 504):
        # Server error
        logger.error(f"API server error: {status}")
        # Circuit breaker will handle
        raise Exception(f"Server error: {status}")
    
    elif status == 499:
        # Timeout
        logger.warning("API timeout")
        raise Exception("Request timeout")
    
    else:
        logger.error(f"Unexpected status: {status}")
        raise Exception(f"Unexpected status: {status}")
```

### 6.4. Exponential Backoff

```python
def exponential_backoff(retry_count, max_delay=32):
    """Returns delay in seconds."""
    delay = min(2 ** retry_count, max_delay)
    return delay

# Usage
retry_count = 0
max_retries = 5

while retry_count < max_retries:
    try:
        response = make_api_request()
        break  # Success
    except Exception as e:
        retry_count += 1
        if retry_count >= max_retries:
            raise
        
        delay = exponential_backoff(retry_count)
        logger.warning(f"Retry {retry_count}/{max_retries} after {delay}s")
        time.sleep(delay)
```

---

## 7. Production Deployment

### 7.1. Environment Configuration

**Use environment variables for all secrets:**

```bash
# .env file (NEVER commit to Git)
API_FOOTBALL_KEY=your_key_here
DATABASE_URL=postgresql://user:pass@localhost:5432/dbname
REDIS_URL=redis://localhost:6379/0
DAILY_QUOTA=7500
MINUTE_QUOTA=300
```

**Load in application:**
```python
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_FOOTBALL_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")
```

### 7.2. Docker Deployment

**docker-compose.yml:**
```yaml
version: '3.8'

services:
  collector:
    build: .
    env_file: .env
    depends_on:
      - postgres
      - redis
    volumes:
      - ./config:/app/config
    networks:
      - api-football-net
  
  postgres:
    image: postgres:15-alpine
    env_file: .env
    volumes:
      - postgres-data:/var/lib/postgresql/data
    networks:
      - api-football-net
  
  redis:
    image: redis:7-alpine
    networks:
      - api-football-net
  
  mcp-server:
    build: ./mcp
    env_file: .env
    depends_on:
      - postgres
    networks:
      - api-football-net

volumes:
  postgres-data:

networks:
  api-football-net:
```

### 7.3. Monitoring & Alerting

**Key Metrics to Monitor:**
1. Daily quota remaining
2. Per-minute quota remaining
3. Circuit breaker state
4. Job success/failure rate
5. Database write latency
6. Coverage percentage by league

**Alerting Thresholds:**
- 🔴 CRITICAL: Daily quota < 500
- ⚠️ WARNING: Daily quota < 1000
- 🔴 CRITICAL: Circuit breaker OPEN
- ⚠️ WARNING: Job failure rate > 10%
- ℹ️ INFO: Coverage < 95% for any tracked league

### 7.4. Logging

**Structured logging for all operations:**

```python
import logging
import json

logger = logging.getLogger(__name__)

def log_api_call(endpoint, params, status, quota_remaining):
    log_data = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint,
        "params": params,
        "status": status,
        "quota_remaining": quota_remaining
    }
    logger.info(json.dumps(log_data))
```

---

## 8. Verification Checklist

### Subscription & Setup
- [ ] ✅ API-Football Pro plan subscription active
- [ ] ✅ API key stored in environment variable (.env)
- [ ] ✅ Backend proxy implemented (clients don't access API directly)
- [ ] ✅ Base URL configured: `https://v3.football.api-sports.io/`

### Rate Limiting
- [ ] ✅ Client-side rate limiter implemented (Token Bucket)
- [ ] ✅ Response headers tracked (`x-ratelimit-*`, `X-RateLimit-*`)
- [ ] ✅ Alert system for low quota (< 1000)
- [ ] ✅ Job priority system implemented

### Database
- [ ] ✅ All timestamps stored as UTC (TIMESTAMPTZ)
- [ ] ✅ Entity IDs used as primary keys
- [ ] ✅ UPSERT pattern implemented (ON CONFLICT DO UPDATE)
- [ ] ✅ JSONB used for nested/variable data
- [ ] ✅ Media files cached on own CDN

### Live Data
- [ ] ✅ `/fixtures?live=all` used (not per-fixture polling)
- [ ] ✅ Delta detection implemented (don't update unchanged data)
- [ ] ✅ Redis cache for live state
- [ ] ✅ WebSocket push to clients

### Odds (if applicable)
- [ ] ⚠️ Odds retention policy verified with support
- [ ] ✅ Critical odds archived in database
- [ ] ✅ Pagination handled correctly

### Error Handling
- [ ] ✅ Circuit breaker pattern implemented
- [ ] ✅ Exponential backoff for retries
- [ ] ✅ 429 errors trigger automatic pause
- [ ] ✅ Cache fallback for circuit breaker open

### Monitoring
- [ ] ✅ Daily quota usage tracked
- [ ] ✅ API response times logged
- [ ] ✅ Circuit breaker state monitored
- [ ] ✅ Alert system for critical errors

### Production
- [ ] ✅ Environment variables for all config
- [ ] ✅ Docker/docker-compose configured
- [ ] ✅ Backup strategy for database
- [ ] ✅ Log aggregation setup

---

## Emergency Contacts & Resources

### Official Resources
- **Dashboard:** https://dashboard.api-football.com/
- **Documentation:** https://www.api-football.com/documentation-v3
- **Support:** https://www.api-football.com/support

### Community
- **Forum:** Check API-Football community forum for rate limit discussions
- **GitHub Issues:** Search for common problems and solutions

### Monitoring Tools
- **Prometheus:** Metric collection
- **Grafana:** Visualization
- **Sentry:** Error tracking
- **Datadog/New Relic:** APM

---

## Document Revision History

| Date | Version | Changes |
|------|---------|---------|
| 2024-12-12 | 1.0 | Initial version |

---

**Document Version:** 1.0  
**Last Updated:** 2024-12-12  
**Target Plan:** Pro Plan (7,500 requests/day)  
**Status:** Production-Ready
