# API-Football Collector — Production v3.0

Bu doküman, bu chat boyunca adım adım kurduğumuz **config-driven**, **quota-safe**, **Coolify/Docker deploy edilebilir** API-Football v3 veri toplama sisteminin **Production v3.0** mimarisini ve repo yapısını **eksiksiz** özetler.

> Not: Git tag tarafında senin akışın `v0.2.0 → v0.3.0`. Buradaki “Production v3.0”, sistemin **3. faz / production-ready mimari seviyesi** anlamındadır.

---

## 1) Mimari Özet (5 Core Component)

- **Config Layer (`config/`)**: Tüm davranış YAML + ENV ile kontrol edilir. Hard-code yok.
- **Collector Service (`src/collector/` + `src/jobs/`)**: APScheduler daemon; job’ları YAML’den okur, RateLimiter ile API çağırır.
- **Data Layers (PostgreSQL)**:
  - **RAW (`raw.api_responses`)**: API envelope’larını JSONB arşivler (audit/replay).
  - **CORE (`core.*`)**: Normalize iş modeli + UPSERT ile idempotent.
  - **MART (`mart.*`)**: Dashboard + coverage tabloları/view’leri.
- **MCP Layer (`src/mcp/`)**: Read-only sorgu araçları (coverage, db stats, last sync, job status).
- **Operational Layer**: Healthcheck script’leri, structured logs, schema apply on startup, retry/backoff.

---

## 2) Deploy Durumu (Coolify Logs ile Doğrulama)

Coolify loglarına göre sistem doğru şekilde ayağa kalkmış:

- **collector** startup:
  - `[OK] applied 10_injuries.sql`
  - `[OK] applied 11_fixture_details.sql`
  - Job’lar scheduler’a başarıyla eklenmiş (`daily_fixtures_by_date`, `daily_standings`, `injuries_hourly`, `fixture_details_recent_finalize`).
- **postgres**: “database system is ready”
- **live_loop**: `ENABLE_LIVE_LOOP=0` → doğru şekilde disabled.

---

## 3) Repo Klasör Yapısı (Eksiksiz)

### `config/` (Tek gerçek kaynak)

- **`config/api.yaml`**: API base_url, timeout, api_key env adı.
- **`config/rate_limiter.yaml`**: günlük/dakika limitleri, emergency stop threshold.
- **`config/coverage.yaml`**: coverage ağırlıkları + (fixtures için) expected count.
- **`config/jobs/`**
  - **`static.yaml`**: `/timezone`, `/countries` (enabled) + `bootstrap_leagues/teams` (default disabled).
  - **`daily.yaml`**: üretim job’ları + `tracked_leagues` + `season`.
  - **`live.yaml`**: `live_fixtures_all` (default disabled).
- **`config/league_targets.txt` + `config/league_overrides.yaml`**: 83 lig çözümleme/override süreci için.

### `db/schemas/` (DB şemaları)

- **`raw.sql`**: `raw.api_responses` (immutable arşiv)
- **`core.sql`**: core model (countries/timezones/leagues/teams/venues/fixtures/standings + fixture_details JSONB)
- **`mart.sql`**: `mart.daily_fixtures_dashboard`, `mart.live_score_panel`, `mart.coverage_status` (TABLE)
- **`10_injuries.sql`**: `core.injuries` (current injuries)
- **`11_fixture_details.sql`**: `core.fixture_players`, `core.fixture_events`, `core.fixture_statistics`, `core.fixture_lineups`
- **`scripts/apply_schemas.py`**: container start’ında idempotent schema apply (Coolify persistent volume sorunlarını çözer)

### `src/` (Production runtime kodu)

#### `src/collector/`
- **`api_client.py`**: **GET-only**, **x-apisports-key only**, async httpx
- **`rate_limiter.py`**: token bucket + header-based quota update + emergency stop
- **`scheduler.py`**: APScheduler; cron timezone = `SCHEDULER_TIMEZONE`

#### `src/jobs/`
- **`static_bootstrap.py`**: timezones/countries/leagues/teams bootstraps
- **`incremental_daily.py`**: scheduler → daily_sync / standings sync entegrasyonu
- **`injuries.py`**: `/injuries` hourly (RAW+CORE+MART coverage)
- **`fixture_details.py`**: per-fixture endpoints (players/events/statistics/lineups) + finalize + optional backfill

#### `src/transforms/`
- fixtures/standings/leagues/teams/venues/timezones/countries + **injuries** + **fixture_endpoints**

#### `src/utils/`
- **`db.py`**: pool + `upsert_raw`, `upsert_core`, `upsert_mart_coverage`
- **`dependencies.py`**: **leagues/teams/venues dependency bootstrap** (FK kırılmasını engeller)
- **`venues_backfill.py`**: `VENUES_BACKFILL_MAX_PER_RUN` ile bounded backfill
- `config.py`, `logging.py`, `standings.py`

### `scripts/` (Operasyon / CLI)

- **`daily_sync.py`**: fixtures by date (RAW→CORE→MART + dependencies + venue backfill)
- **`standings_sync.py`**: standings sync
- **`live_loop.py`**: canlı fixtures poll (opsiyonel)
- **`resolve_tracked_leagues.py`**: lig isimlerini id/season’a mapler (quota-safe)
- **`healthcheck_collector.py`**, **`healthcheck_live_loop.py`**
- `coverage_report.py`, `test_api.py`, `bootstrap.py`, `venues_backfill.py`

### `tests/` (Quality gate)

- **unit tests**: api client, rate limiter, transforms, coverage calculator, delta detector…
- **integration tests**: bootstrap/daily_sync/live_loop/standings_sync
- **mcp tests**: MCP tool’ları

---

## 4) Job Sistemi (Config-Driven)

### Static Bootstrap (`config/jobs/static.yaml`)

- **enabled**
  - `bootstrap_timezones` → `GET /timezone` (monthly)
  - `bootstrap_countries` → `GET /countries` (monthly)
- **disabled (opsiyonel)**
  - `bootstrap_leagues`, `bootstrap_teams` (season+tracked league gerektirir; assumptions yok)

### Daily / Operational (`config/jobs/daily.yaml`)

- **`daily_fixtures_by_date`**: hourly → `/fixtures?date=...` (UTC date runtime hesaplanır)
- **`daily_standings`**: daily
- **`injuries_hourly`**: hourly → `/injuries?league&season` (**current-only**)
- **`fixture_details_recent_finalize`**: 15 dakikada bir
  - biten maçlar (son 24h) için players/events/statistics/lineups tek sefer
  - kickoff penceresinde lineups çekme (lineups gecikebilir)
- **`fixture_details_backfill_90d`**: **disabled** (quota gözlemi sonrası açılacak)

---

## 5) Veri Toplama Stratejisi (Model için)

### Injuries (current-only)
- Endpoint: `GET /injuries?league=<id>&season=<season>`
- Sıklık: hourly (cron) — cron timezone: `SCHEDULER_TIMEZONE=Europe/Istanbul`
- Yazım:
  - RAW: `raw.api_responses`
  - CORE: `core.injuries` (UPSERT, `injury_key` deterministik)
  - MART: `mart.coverage_status` (per league/season endpoint = `/injuries`)

### Fixture-level features (90 gün penceresi)
Per-fixture endpoint’ler:
- `GET /fixtures/players?fixture=<id>`
- `GET /fixtures/events?fixture=<id>`
- `GET /fixtures/statistics?fixture=<id>`
- `GET /fixtures/lineups?fixture=<id>`

Yazım:
- RAW: `raw.api_responses`
- CORE:
  - `core.fixture_players` (PK: `fixture_id, team_id, player_id`)
  - `core.fixture_events` (PK: `fixture_id, event_key`)
  - `core.fixture_statistics` (PK: `fixture_id, team_id`)
  - `core.fixture_lineups` (PK: `fixture_id, team_id`)
- MART:
  - `mart.coverage_status` (rolling 90d coverage endpoint bazlı)

Backfill / operational ayrımı:
- **Recent finalize**: son 24h biten maçlar için full snapshot + kickoff window’da lineups.
- **Backfill 90d**: completed fixtures için rolling 90d, batch ile bounded (disabled by default).

---

## 6) Quota / Rate Limit (Prod güvenliği)

- **Token bucket**: `minute_soft_limit / 60` refill rate
- **Header tracking**: `x-ratelimit-requests-remaining`, `X-RateLimit-Remaining`
- **Emergency stop**: daily remaining threshold altına düşünce `EmergencyStopError`
- **Envelope errors**: API 200 dönse bile `errors.rateLimit` yakalanır (backoff/retry)

---

## 7) Veri Yazma Kuralları (Idempotent + UTC)

- **RAW**: her çağrı `raw.api_responses` içine yazılır (JSONB full envelope)
- **CORE**: tüm tablolar **UPSERT** (`INSERT ... ON CONFLICT DO UPDATE`)
- **UTC zorunlu**: DB TIMESTAMPTZ; scheduler cron timezone ayrı (`SCHEDULER_TIMEZONE`)

### FK Integrity Notu (Fixtures → Venues)
Üretimde gözlemlenen gerçek durum: `/fixtures` response’ları `fixture.venue.id` döndürebilir ama bu venue daha önce `core.venues`’e yazılmamış olabilir.

Bu nedenle:
- `ensure_fixtures_dependencies()` artık **fixtures envelope içinden venue** çıkarır ve `core.venues`’e UPSERT eder (ekstra API çağrısı yok).
- `fixture.venue.id=0` (unknown) geldiğinde `core.fixtures.venue_id` **NULL** yazılır; böylece FK ihlali oluşmaz.

Sonuç:
- `fixtures_venue_id_fkey` hataları ortadan kalkar.
- RAW arşiv (JSONB) değişmeden kalır; CORE idempotent biçimde genişler.

---

## 8) Coolify / Docker Deploy

### Compose (repo root `docker-compose.yml`)

- **postgres**: volume persistent
- **redis**: opsiyonel (live_loop için gerekli)
- **collector**: `python scripts/apply_schemas.py && python -m src.collector.scheduler`
- **live_loop**: `ENABLE_LIVE_LOOP=1` ise çalışır, değilse idle
- **mcp**: read-only query server (Coolify için HTTP/SSE; Claude Desktop için stdio)

### Coolify Environment Variables (örnek)

- **API / DB**
  - `API_FOOTBALL_KEY=...`
  - `DATABASE_URL=postgresql://postgres:postgres@postgres:5432/api_football`
  - `POSTGRES_HOST=postgres`
  - `POSTGRES_PORT=5432`
  - `POSTGRES_USER=postgres`
  - `POSTGRES_PASSWORD=postgres`
  - `POSTGRES_DB=api_football`
- **Scheduler**
  - `SCHEDULER_TIMEZONE=Europe/Istanbul`
- **Live loop**
  - `ENABLE_LIVE_LOOP=0`
  - `REDIS_URL=redis://redis:6379/0`
- **Bounded collection**
  - `FIXTURE_DETAILS_BACKFILL_BATCH=25`
  - `FIXTURE_DETAILS_FINALIZE_BATCH=50`
  - `FIXTURE_LINEUPS_WINDOW_BATCH=50`
  - `VENUES_BACKFILL_MAX_PER_RUN=5`
  - `BACKFILL_FIXTURES_MAX_TASKS_PER_RUN=6`
  - `BACKFILL_FIXTURES_MAX_PAGES_PER_TASK=6` *(window sayısı; ENV adı backward-compat için değişmedi)*
  - `BACKFILL_FIXTURES_WINDOW_DAYS=30`

#### MCP (Coolify) — HTTP/SSE (production)

Bu kurulumla MCP ayrı bir servis olarak deploy edilir ve **HTTP üzerinden erişilebilir** (read-only).

- **Domain örneği**: `mcp.zinalyze.pro`
- **Transport**: `MCP_TRANSPORT=sse`
- **İç port (container)**: `FASTMCP_PORT=8000`
- **Dış port (host)**: `MCP_HOST_PORT=8001` (host’ta 8000 doluysa çakışmayı engeller)

Coolify env’e ekle/koru:
- **Transport seçimleri**
  - `MCP_TRANSPORT=sse` (Coolify için önerilen)
  - `MCP_MOUNT_PATH=` (boş bırak; default `/`)
- **FASTMCP server**
  - `FASTMCP_HOST=0.0.0.0`
  - `FASTMCP_PORT=8000`
  - `FASTMCP_LOG_LEVEL=INFO`
- **Port mapping (host)**
  - `MCP_HOST_PORT=8001`

FastMCP default path’leri (SSE transport):
- **SSE stream**: `/<mount_path>/sse` (default: `/sse`)
- **messages endpoint**: `/<mount_path>/messages/` (default: `/messages/`)
- **streamable-http**: `/<mount_path>/mcp` (default: `/mcp`) *(opsiyonel, biz SSE kullanıyoruz)*

Hızlı doğrulama (örnek):

```bash
curl -i https://mcp.zinalyze.pro/
curl -iN https://mcp.zinalyze.pro/sse
```

> Not: SSE endpoint `curl -N` ile açık bağlantı bekler; bazı proxy ayarlarında zaman aşımı normaldir. Asıl doğrulama, MCP client’ın bağlanması ve tool çağrısı yapmasıdır.

#### Coolify deploy öncesi checklist (önerilen)

- **GitHub**
  - `git status` temiz mi?
  - main branch’e pushlandı mı?
  - tag (opsiyonel): `v0.3.x`
- **Coolify**
  - Env’ler tanımlı mı? (özellikle `DATABASE_URL`, `API_FOOTBALL_KEY`, `SCHEDULER_TIMEZONE`, MCP için `MCP_HOST_PORT`)
  - Domain routing doğru mu? (collector vs mcp ayrı domain)
  - Port çakışması yok mu? (MCP 8001)
  - Deploy sonrası loglarda:
    - collector: `scheduler_started`
    - collector: `[OK] applied 10_injuries.sql` + `[OK] applied 11_fixture_details.sql` (ilk start/upgrade)
    - mcp: uvicorn “running on …” benzeri log (SSE transport)

---

## 9) Redis Uyarısı (Host ayarı)

Redis log uyarısı:
- `WARNING Memory overcommit must be enabled! ... vm.overcommit_memory = 1`

Çözüm (host üzerinde):

```bash
sudo sysctl vm.overcommit_memory=1
echo "vm.overcommit_memory=1" | sudo tee -a /etc/sysctl.conf
```

---

## 10) Üretimde Doğrulama (Checklist)

- **Scheduler çalışıyor mu?**
  - collector log: `scheduler_started` + `job_scheduled` satırları
- **Schema apply oldu mu?**
  - collector log: `[OK] applied 10_injuries.sql`, `[OK] applied 11_fixture_details.sql`
- **RAW akıyor mu?**
  - `raw.api_responses` count artıyor mu?
- **CORE doluyor mu?**
  - `core.fixtures`, `core.injuries`, `core.fixture_*` tablolarda satır artışı
- **Coverage yazılıyor mu?**
  - `mart.coverage_status` satırları oluşuyor mu?

Ek doğrulama (fixtures backfill):
- Log’larda `fixtures_backfill_core_upserted` ve `fixtures_backfill_completed_all_windows` görülmeli.
- Log gürültüsü kontrolü: `venues_upserted_dependency` artık backfill task başına 1 kez loglanır (window başına spam yok).

---

## 11) Bilinen Not (MCP)

MCP tarafında:
- Daha önceki indentation kaynaklı crash riski giderildi.
- MCP artık yeni tabloları da kapsar: `core.injuries`, `core.fixture_players`, `core.fixture_events`, `core.fixture_statistics`, `core.fixture_lineups`.

Deploy öncesi hızlı doğrulama:

```bash
python -m py_compile src/mcp/server.py
pytest -q tests/mcp/test_mcp_tools.py
```


