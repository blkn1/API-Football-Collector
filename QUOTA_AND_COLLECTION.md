# Quota + Collection Cadence (Production v3.1)

Bu doküman, **75.000/gün** quota ile sistemin **hangi job'ın ne sıklıkta** çalıştığını, **hangi ENV ile hızının ayarlandığını**, ve **yaklaşık istek tüketimini** açıklar.

**v3.1 Değişiklikleri:**
- Auto-finish SQL bug fix ve enhancement (try_fetch_first support)
- Verification job eklendi (auto_finish_verification, quota guard ile)
- Schema: `needs_score_verification` kolonu (score verification tracking)

## 1) Quota varsayımları

- **Daily limit (hard)**: 75.000 / gün
- **Daily usage target (policy)**: %90 → **67.500 / gün**
- **Minute limit (hard)**: 300 / dakika
- **Minute soft limit (working cap)**: `config/rate_limiter.yaml -> minute_soft_limit` (token bucket)
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
- `daily_fixtures_by_date` → `GET /fixtures?...` (**günde 1 kez**, TR 06:00)
  - **Mod**: `config/jobs/daily.yaml -> fixtures_fetch_mode = per_tracked_leagues`
  - **Çağrı şekli**: `/fixtures?league=<id>&season=<season>&date=YYYY-MM-DD` (tracked ligler için)
  - **RAW**: `/fixtures`
  - **CORE**: `core.fixtures`
  - Not: `global_by_date` mod desteği dokümanda geçebilir ama bu deployment’ta kullanılmıyor.

- `fixture_details_recent_finalize` → per-fixture detail endpoint’leri (**günde 1 kez**, TR 06:30)
  - `/fixtures/players`, `/fixtures/events`, `/fixtures/statistics`, `/fixtures/lineups`
  - **CORE**: `core.fixture_players/events/statistics/lineups`
- `daily_standings` → `GET /standings?league&season` (**per-league season**, günlük)
  - **Scope policy**: Cup competition’larda `/standings` default **out-of-scope** olabilir (request atılmaz).
  - **RAW**: `/standings`
  - **CORE**: `core.standings` (league+season bazında replace)
- `injuries_hourly` → `GET /injuries?league&season` (**per-league season**, saatlik)
  - **RAW**: `/injuries`
  - **CORE**: `core.injuries`

- `top_scorers_daily` → `GET /players/topscorers?league&season` (**günde 1 kez**, TR 06:40)
  - **Scope policy**: Cup competition’larda `/players/topscorers` default out-of-scope olabilir.
  - **RAW**: `/players/topscorers`
  - **CORE**: `core.top_scorers`

- `team_statistics_refresh` → `GET /teams/statistics?league&season&team` (**10 dakikada bir**, gün içine yayılmış)
  - **Scope policy**: Cup competition’larda `/teams/statistics` default out-of-scope olabilir.
  - **RAW**: `/teams/statistics`
  - **CORE**: `core.team_statistics` (+ progress: `core.team_statistics_progress`)

### 3.3 Backfill (DB doldurma, live_loop yok)
Backfill state tablosu:
- `core.backfill_progress` (resume edilebilir backfill)

Backfill sezonları (SeçenekB, lig bazlı doğru sezon):
- `config/jobs/daily.yaml tracked_leagues[].season` = **current**
- Varsayılan backfill pairs = **(league, current)** + **(league, current-1)**
- `backfill.seasons` artık kullanılmıyor (global cross-product çok maliyetliydi).

#### Fixtures backfill (en ağır)
- Job: `fixtures_backfill_league_season`
- Endpoint (windowed): `GET /fixtures?league=<id>&season=<season>&from=YYYY-MM-DD&to=YYYY-MM-DD`
- Fallback (rare): Eğer `core.leagues.seasons` içinde sezon başlangıç/bitiş tarihleri yoksa tek sefer “unbounded” olabilir:
  - `GET /fixtures?league=<id>&season=<season>`
  - Bu risk `ensure_league_exists` refresh + `/leagues?id=...` ile minimize edildi.
- Sıklık: **10 dakikada bir** (cron `0-59/10 * * * *`)
- Resume: `core.backfill_progress.next_page` (**window index** olarak kullanılır)

#### Standings backfill (ucuz)
- Job: `standings_backfill_league_season`
- Endpoint: `GET /standings?league=<id>&season=<season>`
- Sıklık: **10 dakikada bir**
- Resume: `core.backfill_progress.completed=true`

#### 90 gün fixture details backfill
- Job: `fixture_details_backfill_90d`
- Sıklık: 10 dakikada bir (batch ile bounded)

### 3.4 Stale fixture finalization (hibrit: auto-finish + refresh)

**Otomatik çözüm: Donmuş live maçlarını FT'ye geçirme**

Bu deployment, live pipeline olmadan bile maçların FT'ye geçebilmesi için **hibrit yaklaşım** kullanır:

**Aşama 1: Auto-finish (DB-only, opsiyonel API fetch)**
- Job: `auto_finish_stale_fixtures`
- Sıklık: **Her saat başı** (cron `0 * * * *`)
- Threshold: `threshold_hours=2`, `safety_lag_hours=3`
- Logic:
  - **Varsayılan (try_fetch_first=false)**: DB'de direkt status güncelleme: `status_short = 'FT'`, `needs_score_verification = TRUE`
  - **Opsiyonel (try_fetch_first=true)**: Önce batch API fetch dener (`GET /fixtures?ids=...`, max 20 per request)
    - Başarılı: UPSERT ile güncel skorları yazar, `needs_score_verification = FALSE`
    - Başarısız: DB-only update yapar, `needs_score_verification = TRUE`
  - Double-threshold güvenlik:
    - `date < NOW() - 2 hours` (maç 2 saatten önce başlamış)
    - `updated_at < NOW() - 3 hours` (son güncelleme 3 saatten önce)
  - SQL bug fix (v3.1): `league_id = ANY(%s)` WHERE clause eklendi, parametre sırası düzeltildi
  - Scope: `config/jobs/daily.yaml -> tracked_leagues`
  - Batch limit: `max_fixtures_per_run=1000` (default)
- API çağrısı:
  - `try_fetch_first=false` (default): **YOK** (quota tüketimi 0)
  - `try_fetch_first=true`: Batch fetch (100 fixture = 5 request, quota tüketimi ~5-50 req/run)
- Coverage: NS, HT, 2H, 1H, LIVE, BT, ET, P, SUSP, INT durumları
- Güvenlik: Transaction wrapper (rollback on error), dry-run mode

**Aşama 2: Stale refresh (API çağrılı, kalan durumlar için)**
- Job: `stale_live_refresh`
- Sıklık: **Her 5 dakikada bir** (cron `*/5 * * * *`)
- Threshold: `stale_threshold_minutes=15` (auto-finish'den daha agresif)
- Logic:
  - API çağrısı: `GET /fixtures?ids=<id1>-<id2>-...` (max 20)
  - RAW + CORE UPSERT (status update)
  - Scope: `scope_source=daily` → `tracked_leagues`
  - Batch size: `batch_size=20`
- Coverage: 1H, 2H, HT, ET, BT, P, LIVE, SUSP, INT (auto-finish'ten kalanlar)
- API çağrısı: Var (quota tüketimi <50/run beklenir)

**Aşama 1.5: Verification job (auto-finished maçların skorlarını doğrulama)**
- Job: `auto_finish_verification`
- Sıklık: **Her 30 dakikada bir** (cron `*/30 * * * *`)
- Threshold: `min_daily_quota=50000` (quota guard)
- Logic:
  - `needs_score_verification = TRUE` ve `status_short = 'FT'` olan fixture'ları seçer
  - Batch fetch: `GET /fixtures?ids=...` (max 20 per request)
  - RAW + CORE UPSERT ile güncel skorları yazar
  - Başarılı olanlarda `needs_score_verification = FALSE` yapar
  - Quota guard: Sadece `daily_remaining >= min_daily_quota` olduğunda çalışır
- API çağrısı: Var (batch fetch, beklenen <200 req/day)
- Quota tüketimi:
  - Worst-case (her run 200 fixture): 480 req/day
  - Gerçek beklenen: **<200 req/day** (quota guard sayesinde)

**Aşama 3: Stale scheduled finalize (NS/TBD durumları için)**
- Job: `stale_scheduled_finalize`
- Sıklık: **Her 30 dakikada bir** (cron `*/30 * * * *`)
- Threshold: `stale_threshold_minutes=60`, `lookback_days=3`
- Logic:
  - API çağrısı: `GET /fixtures?ids=...` (max 20)
  - RAW + CORE UPSERT
  - Coverage: NS, TBD durumları
- API çağrısı: Var

**Hibrit yaklaşım avantajları:**
- Auto-finish: **Quota verimli** (default: API çağrısı yok), en çok stale'ı halleder
- Verification: **Skor doğrulama** (quota guard ile güvenli, batch fetch ile verimli)
- Stale refresh: **Kalan durumlar** için güvenlik ağı (recent updates için)
- Stale scheduled: **NS/TBD** için özel coverage
- Overall: Live pipeline olmadan da sistemin kendi kendini FT'ye geçirebilmesi ve skorları doğrulayabilmesi

**Monitoring:**
- MCP: `stale_fixtures_report(threshold_hours=2, safety_lag_hours=3)` → auto-finish öncesi stale'ları gör
- MCP: `auto_finish_stats(hours=24)` → son 24 saatte kaç maç auto-finished'i gör
- MCP: `get_job_status(job_name="auto_finish_verification")` → verification job durumu
- MCP: `get_stale_live_fixtures_status(threshold_minutes=15)` → stale refresh kalanlarını gör

## 4) Hız kontrolü (Coolify ENV)

Bu ENV’ler backfill hızını ayarlar (quota-safe):

### 4.1 Fixtures backfill (windowing)
- `BACKFILL_FIXTURES_MAX_TASKS_PER_RUN` (default: **6**)  
  Aynı çalıştırmada kaç (league,season) işlenecek.
- `BACKFILL_FIXTURES_MAX_PAGES_PER_TASK` (default: **6**)  
  Her (league,season) için kaç **window** çekilecek. (ENV adı backward-compat için değişmedi.)
- `BACKFILL_FIXTURES_WINDOW_DAYS` (default: **30**, prod’da **14** önerilir)  
  Her window kaç gün kapsasın. Küçük değer = daha çok request, daha granular backfill.

Yaklaşık istek/dk:
- Maks `tasks * windows` (ör: 6*6=36 request/run).  
Job her 1 dk çalıştığı için ≈ **36 req/min** (sadece fixtures backfill).

Notlar:
- Window içinde fixture sayısı artınca DB upsert maliyeti artar ama API request sayısı değişmez.
- `window_days` küçülürse aynı sezonu bitirmek için daha fazla window gerekir (daha fazla toplam request).

### 4.2 Standings backfill
- `BACKFILL_STANDINGS_MAX_TASKS_PER_RUN` (default: **2**)  
10 dakikada bir 2 request → ≈ **0.2 req/min**.

### 4.3 Stale fixture finalization (hibrit yaklaşım)

**Auto-finish (quota-efficient):**
- Job: `auto_finish_stale_fixtures`
- Sıklık: **1 saatte 1 kez** (cron `0 * * * *`)
- API çağrısı:
  - `try_fetch_first=false` (default): **YOK** (DB-only update)
  - `try_fetch_first=true`: Batch fetch (100 fixture = 5 request)
- Quota tüketimi:
  - Default: **0 req/hour** ≈ **0 req/day**
  - `try_fetch_first=true`: **~5-50 req/hour** ≈ **120-1200 req/day** (değişken)
- İş yükü: DB write-only (UPSERT core.fixtures) veya API fetch + DB UPSERT

**Verification (quota-guarded):**
- Job: `auto_finish_verification`
- Sıklık: **30 dakikada 1 kez** (cron `*/30 * * * *`)
- Quota guard: Sadece `daily_remaining >= 50000` olduğunda çalışır
- API çağrısı: Batch fetch (max 20 ids per request)
- Quota tüketimi: **<200 req/day** (quota guard sayesinde kontrollü)

**Stale refresh (API-based):**
- Job: `stale_live_refresh`
- Sıklık: **5 dakikada bir** (cron `*/5 * * * *`)
- Threshold: `stale_threshold_minutes=15`
- Batch size: `max 20 ids/batch`
- API çağrısı: Değişken, beklenen **<50 req/day** (quota verimli)
- Quota tüketimi:
  - Worst-case (her run 20 ids): 288 req/day = **288 req/day**
  - Gerçek beklenen (genelde boş veya az sayı): **<50 req/day**
  - Auto-finish ile birlikte: **Toplam <100 req/day**

**Stale scheduled finalize (NS/TBD):**
- Job: `stale_scheduled_finalize`
- Sıklık: **30 dakikada bir** (cron `*/30 * * * *`)
- Threshold: `stale_threshold_minutes=60`
- Batch size: `max 20 ids/batch`
- API çağrısı: Değişken, beklenen **<30 req/day**
- Quota tüketimi:
  - Worst-case (her run 20 ids): 48 req/day
  - Gerçek beklenen (NS/TBD nadir): **<30 req/day**

**Overall quota impact (hibrit):**
- Total daily (default, try_fetch_first=false):
  - Auto-finish: **0 req/day**
  - Verification: **<200 req/day** (quota guard ile)
  - Stale refresh: **<100 req/day**
  - Stale scheduled: **<30 req/day**
  - **Toplam: <330 req/day** (<0.5% of daily quota)
- Total daily (try_fetch_first=true):
  - Auto-finish: **120-1200 req/day** (değişken)
  - Verification: **<200 req/day**
  - Stale refresh: **<100 req/day**
  - Stale scheduled: **<30 req/day**
  - **Toplam: <1530 req/day** (<2.1% of daily quota)
- Daily budget: 75,000 req/day
- Usage: **<2.1% of daily quota** (çok verimli)
- Benefit: Auto-finish ile en çok stale'ı API çağrısı olmadan halleder, verification ile skorları doğrular

**Quota optimization:**
- Auto-finish: **Default: API çağrısı yok** → quota verimliliği maksimum
- Verification: **Quota guard** ile sadece quota bolken çalışır → güvenli
- Stale refresh: Sadece auto-finish'ten kalan durumlar için → minimum API kullanımı
- Hibrit yaklaşım: Live pipeline olmadan da sistemin kendi kendini FT'ye geçirebilmesi ve skorları doğrulayabilmesi

### 4.4 Fixture details
Bu job’lar per-fixture 4 endpoint çağırır:
- `FIXTURE_DETAILS_BACKFILL_BATCH` (default: 25 fixture/run)
- `FIXTURE_DETAILS_FINALIZE_BATCH` (default: 50)
- `FIXTURE_LINEUPS_WINDOW_BATCH` (default: 50)

Not: rate limiter shared olduğu için toplam request/min hiçbir zaman `minute_soft_limit` üstünde koşamaz; bu değer aşılırsa token bucket bekletir.

### 4.4 Dakikalık rateLimit (API) için kalıcı önlem

- **Token bucket**: `src/collector/rate_limiter.py` (startup burst engellendi: bucket default 0 token ile başlar)
- **/teams cache**: `core.team_bootstrap_progress`  
  Aynı `(league_id, season)` için `/teams` bir kere başarılı çalışınca tekrar çağrılmaz.

## 5) Sizin Coolify ENV setiniz (özet)

Zorunlu / kritik:
- `API_FOOTBALL_KEY`
- `DATABASE_URL` (veya `POSTGRES_*`)
- `SCHEDULER_TIMEZONE=Europe/Istanbul`
Not: Bu deployment’ta live polling servisleri compose’tan kaldırıldı (ENABLE_LIVE_LOOP kullanılmıyor).

## 5.1 Cron beklemeden doğrulama (manual test)
Collector terminal:
- Tek lig top scorers:
  - `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=top_scorers_daily python3 scripts/run_job_once.py`

Postgres terminal (kanıt):
- `psql -U postgres -d api_football -c "SELECT COUNT(*) FROM core.top_scorers;"`

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
- Teams dependency cache: `core.team_bootstrap_progress` (completed oranı, last_error)
- CORE doluluk: MCP `get_database_stats()` (fixtures, standings, injuries, fixture_* tabloları)


