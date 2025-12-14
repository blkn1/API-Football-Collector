# Quota + Collection Cadence (Production)

Bu doküman, **75.000/gün** quota ile sistemin **hangi job’ın ne sıklıkta** çalıştığını, **hangi ENV ile hızının ayarlandığını**, ve **yaklaşık istek tüketimini** açıklar.

## 1) Quota varsayımları

- **Daily limit (hard)**: 75.000 / gün
- **Daily usage target (policy)**: %90 → **67.500 / gün**
- **Minute limit (hard)**: 300 / dakika
- **Minute soft limit (working cap)**: 280 / dakika (sistem içi token bucket)
- **Emergency stop**: `daily_remaining < 7.500` olduğunda tüm job’lar stop (quota buffer)

Kaynak config:
- `config/api.yaml`
- `config/rate_limiter.yaml`

## 2) Data flow (RAW → CORE → MART)

- **RAW**: `raw.api_responses` (tam envelope JSONB arşiv)
- **CORE**: normalize tablolar (UPSERT/idempotent)
- **MART**: `mart.coverage_status` + materialized view’ler (dashboard)

## 3) Job’lar ve sıklıkları (scheduler)

Kaynak: `config/jobs/*.yaml` + `src/collector/scheduler.py`

### 3.1 Static bootstrap (nadiren)
- `bootstrap_timezones` → `GET /timezone` (aylık)
- `bootstrap_countries` → `GET /countries` (aylık)

### 3.2 Günlük/operasyonel (her gün açık)
- `daily_fixtures_by_date` → `GET /fixtures?date=YYYY-MM-DD` (saatlik)
  - **RAW**: `/fixtures`
  - **CORE**: `core.fixtures` (+ bazı nested bloklar gelirse `core.fixture_details` JSONB)
- `daily_standings` → `GET /standings?league&season` (günlük)
  - **RAW**: `/standings`
  - **CORE**: `core.standings` (league+season bazında replace)
- `injuries_hourly` → `GET /injuries?league&season` (saatlik)
  - **RAW**: `/injuries`
  - **CORE**: `core.injuries`
- `fixture_details_recent_finalize` (15 dk)
  - Biten maçlar (son 24h): per-fixture
    - `/fixtures/players`, `/fixtures/events`, `/fixtures/statistics`, `/fixtures/lineups`
  - **CORE**:
    - `core.fixture_players`
    - `core.fixture_events`
    - `core.fixture_statistics`
    - `core.fixture_lineups`

### 3.3 Backfill (DB doldurma, live_loop yok)
Backfill state tablosu:
- `core.backfill_progress` (resume edilebilir backfill)

Backfill sezonları:
- `config/jobs/daily.yaml` → `backfill.seasons: [2023, 2024, 2025]`

#### Fixtures backfill (en ağır)
- Job: `fixtures_backfill_league_season`
- Endpoint: `GET /fixtures?league=<id>&season=<season>&page=<n>`
- Sıklık: **her 1 dakika** (cron `* * * * *`)
- Resume: `core.backfill_progress.next_page`

#### Standings backfill (ucuz)
- Job: `standings_backfill_league_season`
- Endpoint: `GET /standings?league=<id>&season=<season>`
- Sıklık: **10 dakikada bir**
- Resume: `core.backfill_progress.completed=true`

#### 90 gün fixture details backfill
- Job: `fixture_details_backfill_90d`
- Sıklık: 10 dakikada bir (batch ile bounded)

## 4) Hız kontrolü (Coolify ENV)

Bu ENV’ler backfill hızını ayarlar (quota-safe):

### 4.1 Fixtures backfill (paging)
- `BACKFILL_FIXTURES_MAX_TASKS_PER_RUN` (default: **6**)  
  Aynı çalıştırmada kaç (league,season) işlenecek.
- `BACKFILL_FIXTURES_MAX_PAGES_PER_TASK` (default: **6**)  
  Her (league,season) için kaç sayfa çekilecek.

Yaklaşık istek/dk:
- Maks `tasks * pages` (ör: 6*6=36 request/run).  
Job her 1 dk çalıştığı için ≈ **36 req/min** sadece fixtures backfill.

### 4.2 Standings backfill
- `BACKFILL_STANDINGS_MAX_TASKS_PER_RUN` (default: **2**)  
10 dakikada bir 2 request → ≈ **0.2 req/min**.

### 4.3 Fixture details
Bu job’lar per-fixture 4 endpoint çağırır:
- `FIXTURE_DETAILS_BACKFILL_BATCH` (default: 25 fixture/run)
- `FIXTURE_DETAILS_FINALIZE_BATCH` (default: 50)
- `FIXTURE_LINEUPS_WINDOW_BATCH` (default: 50)

Not: rate limiter shared olduğu için toplam request/min hiçbir zaman `minute_soft_limit` üstünde koşamaz; bu değer aşılırsa token bucket bekletir.

## 5) Sizin Coolify ENV setiniz (özet)

Zorunlu / kritik:
- `API_FOOTBALL_KEY`
- `DATABASE_URL` (veya `POSTGRES_*`)
- `SCHEDULER_TIMEZONE=Europe/Istanbul`
- `ENABLE_LIVE_LOOP=0` (live_loop kapalı)

MCP (opsiyonel, sizde aktif):
- `MCP_TRANSPORT=sse`
- `FASTMCP_PORT=8000`
- `MCP_HOST_PORT=8001`
- Domain: `mcp.zinalyze.pro`

## 6) Prod doğrulama (ne zaman ne kadar çekiyoruz?)

Minimum gözlem metrikleri:
- RAW request hızı: `raw.api_responses` son 1 dakikadaki artış
- Quota: MCP `get_rate_limit_status()` (daily/minute remaining)
- Backfill ilerleme: `core.backfill_progress` satırları (completed oranı, next_page artışı)
- CORE doluluk: MCP `get_database_stats()` (fixtures, standings, injuries, fixture_* tabloları)


