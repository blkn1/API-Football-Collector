## Production Runbook (v3.1)

Bu runbook; çalışan collector + Postgres + MCP + Read API stack'inde **deploy sonrası doğrulama**, **günlük operasyon**, ve **incident** (quota/coverage/data gap) akışlarını tek yerde toplar.

**v3.1 Değişiklikleri:**
- Auto-finish SQL bug fix (league_id WHERE clause, parametre sırası)
- Schema: `needs_score_verification` kolonu eklendi
- Auto-finish enhancement: `try_fetch_first` parametresi (opsiyonel API fetch)
- Verification job: `auto_finish_verification` (quota guard ile)
- Correction script: `fix_auto_finished_scores.py` (one-time data fix)

### 0) Servisler ve roller
- **collector**: config-driven job scheduler, quota-safe API çağrıları, RAW→CORE→MART yazımı
- **postgres**: RAW/CORE/MART source of truth
- **mcp**: read-only gözlem arayüzü (tool’lar)
- **read_api**: read-only REST + SSE (ops ve dış tüketim için okuma katmanı)

---

## 1) MCP Acceptance Test (Prod Smoke)

### 1.1 Önkoşullar
- DB erişimi OK olmalı.
- MCP healthcheck:

```bash
python scripts/healthcheck_mcp.py
```

> Coolify/Docker’da healthcheck container içinde çalışır. Local’de aynı env’lerle çalıştır.

### 1.2 MCP tool çağrıları (minimum set)
Aşağıdaki tool’lar `src/mcp/server.py` içinde tanımlıdır.

- **A) Quota**: `get_rate_limit_status()`
  - Beklenen minimum: `ok=true`, `daily_remaining`/`minute_remaining` (int veya None)
- **B) DB snapshot**: `get_database_stats()`
  - Beklenen minimum: `ok=true`, RAW/CORE sayıları int
- **C) Coverage**: `get_coverage_summary()` (gerekirse `season=...`)
  - Beklenen minimum: `ok=true`, `summary` None olabilir
- **D) Job status**: `get_job_status()`
  - Beklenen minimum: `ok=true`, config job’ları listelenir; log varsa status set edilir
- **E) CORE örnekleme**: `query_fixtures(limit=10)`
  - Beklenen minimum: **envelope** (schema‑driven), örnek shape:
    - `ok=true`
    - `items=[...]`
    - `ts_utc=...`
  - Item alanları (minimum): `id, league_id, date_utc, status, home_team, away_team`

Ek ops gözlemler (Phase 1.5):
- **Backfill progress**: `get_backfill_progress()`
- **RAW error health**: `get_raw_error_summary(since_minutes=60)`
- **Recent error logs**: `get_recent_log_errors(limit=50)`
- **Scope policy (quota optimizasyonu)**: `get_scope_policy(league_id=<LID>)`
- **Stale scheduled (NS/TBD ama geçmiş kickoff)**: `get_stale_scheduled_fixtures_status(threshold_minutes=180, lookback_days=3)`
- **Stale fixtures (live durumunda kalmış maçlar)**: `stale_fixtures_report(threshold_hours=2, safety_lag_hours=3)`
- **Auto-finish stats**: `auto_finish_stats(hours=24)`

### 1.6 Incident: “Geçmiş tarihte NS görünüyor ama FT olmalı”

Semptom:
- Read API `/v1/fixtures?date=YYYY-MM-DD` (veya `/read/fixtures?...&date_from/date_to`) geçmiş gün için fixture döner
- ama `status_short=NS` / `TBD` kalmıştır (FT/AET/PEN’e dönmesi beklenir).

Tipik kök sebep:
- `daily_fixtures_by_date` sync’i UTC günün erken saatinde çalıştı ve fixtures NS iken yazdı
- ama maçlar bittikten sonra status refresh yapılmadı.

Çözüm:
- `config/jobs/daily.yaml` içinde `stale_scheduled_finalize` job’ı **enabled** olmalı.
- Job, stale fixture’ları `GET /fixtures?ids=...` ile yeniden çekip `core.fixtures`’e UPSERT eder (status’ları finalize eder).

Doğrulama:
- MCP: `get_stale_scheduled_fixtures_status(...)` → `stale_count` zamanla 0’a yaklaşmalı.

### 1.3 PASS / FAIL kriteri
- **PASS**: A+B+D `ok=true` ve DB bağlantısı sağlıklı.
- **FAIL**: Tool exception / DB bağlantı hatası / output şeması bozulmuş.

### 1.5 Scope policy doğrulama (Cup vs League)
Bu deployment’ta quota optimizasyonu için **Cup** competition’larda bazı endpoint’ler out-of-scope kabul edilir:
- Varsayılan: Cup → `/standings`, `/teams/statistics`, `/players/topscorers` kapalı
- Baseline her zaman açık: `/fixtures` + fixture_details + `/injuries`

Doğrulama:
- `get_scope_policy(league_id=206)` → Türkiye Kupası için `/standings in_scope=false` beklenir.
- `get_raw_error_summary(since_minutes=60, endpoint="/standings")` → request sayısının düşmesi beklenir (redeploy sonrası).

### 1.4 MCP Prod transport notu (Traefik + streamable-http)
Prod’da MCP `streamable-http` çalışır ve **stateful session** gerektirir:
- `Accept: application/json, text/event-stream`
- `mcp-session-id` header’ı
- Önce `initialize`, sonra `tools/list`

Claude Desktop notu:
- Claude Desktop HTTP transport’ları native konuşmadığı için prod MCP’ye bağlanırken **stdio→streamable-http adapter** kullanın.
- Ayrıntılı config: `MCP_USAGE_GUIDE.md` → “Claude Desktop → Prod MCP (remote) (streamable-http)”.

Hızlı curl doğrulaması (prod):

```bash
curl -i -X POST "https://mcp.zinalyze.pro/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0.1"}}}'
```

> Response header’ından `mcp-session-id` kopyala, sonra:

```bash
curl -i -X POST "https://mcp.zinalyze.pro/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: <PASTE_SESSION_ID>" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"cursor":null}}'
```

---

## 2) DB doğrulama sorguları (read-only)

### 2.1 RAW akışı
```sql
SELECT COUNT(*) FROM raw.api_responses;

SELECT endpoint, COUNT(*) AS cnt, MAX(fetched_at) AS last_fetched
FROM raw.api_responses
GROUP BY endpoint
ORDER BY MAX(fetched_at) DESC;
```

### 2.2 Quota header gözlemi
```sql
SELECT
  fetched_at,
  response_headers->>'x-ratelimit-requests-remaining' AS daily_remaining,
  response_headers->>'X-RateLimit-Remaining' AS minute_remaining
FROM raw.api_responses
WHERE response_headers ? 'x-ratelimit-requests-remaining'
   OR response_headers ? 'X-RateLimit-Remaining'
ORDER BY fetched_at DESC
LIMIT 5;
```

### 2.3 CORE doluluk
```sql
SELECT COUNT(*) FROM core.fixtures;
SELECT COUNT(*) FROM core.standings;
SELECT COUNT(*) FROM core.injuries;
```

### 2.4 Coverage
```sql
SELECT season, COUNT(*) AS rows, MAX(calculated_at) AS last_calculated
FROM mart.coverage_status
GROUP BY season
ORDER BY season DESC;
```

### 2.5 Backfill progress
```sql
SELECT job_id, COUNT(*) AS total, SUM(CASE WHEN completed THEN 1 ELSE 0 END) AS completed
FROM core.backfill_progress
GROUP BY job_id
ORDER BY job_id;

SELECT *
FROM core.backfill_progress
WHERE completed = FALSE
ORDER BY updated_at DESC
LIMIT 50;
```

---

## 3) Read API (REST + SSE)

### 3.1 REST (read-only)
- `GET /v1/health`
- `GET /v1/quota`
- `GET /v1/fixtures?league_id=&date=&status=&limit=`
- `GET /v1/standings/{league_id}/{season}`
- `GET /v1/teams?search=&league_id=&limit=`
- `GET /v1/injuries?league_id=&season=&team_id=&player_id=&limit=`

Curated Feature Store (modelleme / feature engineering):
- `GET /read/leagues?country=&season=&limit=&offset=`
- `GET /read/fixtures?league_id=&country=&season=&date_from=&date_to=&team_id=&status=&limit=&offset=`
- `GET /read/fixtures/{fixture_id}`
- `GET /read/fixtures/{fixture_id}/events|lineups|statistics|players`
- `GET /read/top_scorers?league_id=&season=&include_raw=1&limit=&offset=`
- `GET /read/team_statistics?league_id=&season=&team_id=&include_raw=1&limit=&offset=`
- `GET /read/h2h?team1_id=&team2_id=&league_id=&season=&limit=`
- `GET /read/coverage?season=&league_id=&country=&endpoint=&limit=&offset=`

### 3.2 SSE (read-only)
- `GET /v1/sse/system-status` → event: `system_status`
- `GET /v1/sse/live-scores` → event: `live_score_update`

### 3.3 Access control (prod)
- Basic auth:
  - `READ_API_BASIC_USER`
  - `READ_API_BASIC_PASSWORD`
- IP allowlist (ops):
  - `READ_API_IP_ALLOWLIST` (comma-separated)

### 3.4 Prod Read API URL (Coolify)
Bu repo Read API için sabit bir domain hard-code etmez. Prod’da Read API base URL’ini şu şekilde belirleyin:
- Coolify’de Read API servisine bir domain route atayın (örn: `https://readapi.<domain>`).
- Varsa Coolify otomatik ENV’leri: `SERVICE_URL_READ_API` / `SERVICE_FQDN_READ_API` (bkz. `env_info.md`).

Örnek (placeholder):
```bash
READ_API_BASE="https://<SERVICE_FQDN_READ_API>"
```

Bizim prod örneği:
- `https://readapi.zinalyze.pro`

---

## 4) Günlük operasyon checklist
- **Quota trend**: `get_rate_limit_status()` / `GET /v1/quota`
- **Coverage**: `get_coverage_summary()` + en düşük coverage satırlarını `get_coverage_status(league_id)` ile drilldown
- **Job health**: `get_job_status()` + `get_recent_log_errors()`
- **Stale live kontrolü** (canlı panel güvenilirliği): `get_stale_live_fixtures_status(threshold_minutes=30, tracked_only=true, scope_source="daily")`
- **Data drift**: `get_raw_error_summary(since_minutes=1440)` (son 24h)

---

## 4.1 Daily-only (TR 06:00) acceptance checklist

Bu checklist, canlı + global-by-date kapalıyken (tracked lig + backfill modeli) deploy sonrası doğrulamayı standardize eder.

### 4.1.1 Config doğrulama (redeploy öncesi)
- `config/jobs/daily.yaml`:\n
  - `fixtures_fetch_mode: per_tracked_leagues`\n
  - `tracked_leagues[*].id` ve `tracked_leagues[*].season` dolu\n
- (Öneri) `tracked_leagues[*].name` ASCII/İngilizce tutulabilir (label-only; davranış `id+season`).\n
- ENV:\n
  - `SCHEDULER_TIMEZONE=Europe/Istanbul` (cron TR saatine göre)\n
\n
(Opsiyonel, audit):\n
- Resolver zinciri kullanıyorsan:\n
  - `config/league_targets.txt` + `config/league_overrides.yaml` güncel\n
  - `python3 scripts/resolve_tracked_leagues.py` sonrası `config/resolved_tracked_leagues.yaml` üretilmiş (audit)\n

### 4.1.2 MCP doğrulama (redeploy sonrası)
- `get_job_status()`:\n
  - `daily_fixtures_by_date` enabled ve cron `0 6 * * *`\n
  - `fixtures_backfill_league_season` enabled ve cron `0-59/10 * * * *`\n
  - `fixture_details_backfill_season` enabled ve cron `5-59/10 * * * *`\n
  - `auto_finish_stale_fixtures` enabled ve cron `0 * * * *` (SQL bug fix v3.1, try_fetch_first support)\n
- `auto_finish_verification` enabled ve cron `*/30 * * * *` (v3.1, quota guard ile)\n
  - `stale_live_refresh` enabled ve cron `*/5 * * * *` (re-enabled, daha agresif: 15m threshold, 5m cron)\n
- `get_daily_fixtures_by_date_status(since_minutes=240)`:\n
  - `running=true` (06:00–08:00 arası)\n
  - `requests>0`\n
- `auto_finish_stats(hours=24)`:\n
  - `total_auto_finished` > 0 beklenir (son 24 saatte en az birkaç maç)\n
  - `unique_leagues_affected` tracked league sayısının altında olmalı\n
- `get_job_status(job_name="auto_finish_verification")`:\n
  - Job enabled ve cron `*/30 * * * *` olmalı\n
  - `last_event` güncel olmalı (quota guard nedeniyle bazen skip edilebilir)\n
- `stale_fixtures_report(threshold_hours=2, safety_lag_hours=3)`:\n
  - `stale_count` zamanla düşmeli (auto_finish ile)\n
- `get_backfill_progress(job_id="fixtures_backfill_league_season")`:\n
  - `completed_tasks` zamanla artmalı (her gün kademeli)\n
- `get_rate_limit_status()`:\n
  - `minute_remaining` ve `daily_remaining` anormal düşmemeli\n
- `get_raw_error_summary(since_minutes=60)`:\n
  - `err_4xx/err_5xx/envelope_errors` yükselmiyorsa OK\n

### 4.1.3 DB hızlı doğrulama (ops, read-only)
```sql
SELECT COUNT(*) FROM core.fixtures;
SELECT job_id, COUNT(*) AS total, SUM(CASE WHEN completed THEN 1 ELSE 0 END) AS completed
FROM core.backfill_progress
GROUP BY job_id
ORDER BY job_id;
```

### 4.1.3.1 Cron beklemeden job doğrulama (Coolify terminal)
Collector terminal (tek lig, quota-safe):
- `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=top_scorers_daily python3 scripts/run_job_once.py`

Yeni eklenen lig/kupa için fixtures doğrulama (tek lig):
- `cd /app && ONLY_LEAGUE_ID=<LEAGUE_ID> JOB_ID=daily_fixtures_by_date python3 scripts/run_job_once.py`
  - Not: API key/quota gerekir; job bugünün UTC tarihini kullanır (`/fixtures?league&season&date`).\n

Postgres terminal (kanıt):
- `psql -U postgres -d api_football -c "SELECT COUNT(*) FROM raw.api_responses WHERE endpoint='/players/topscorers' AND fetched_at > NOW() - INTERVAL '1 hour';"`
- `psql -U postgres -d api_football -c "SELECT COUNT(*) FROM core.top_scorers;"`

Not: Postgres terminal bir shell’dir; SQL yazmak için `psql` kullanmalısın.

### 4.1.4 Test doğrulama (pytest + idempotency)
Minimum unit set (hızlı, quota-safe):
```bash
pytest -q tests/unit/test_rate_limiter.py
pytest -q tests/unit/test_api_client.py
pytest -q tests/unit/test_daily_sync_dry_run.py
```

Opsiyonel integration set (docker gerekir; RAW+CORE yazımını doğrular):
```bash
pytest -q -m integration tests/integration/test_bootstrap.py
pytest -q -m integration tests/integration/test_daily_sync.py
```

Idempotency kontrolü (DB):\n
- Aynı fixture’ı tekrar tekrar ingest etmek **CORE’da satır sayısını şişirmez** (UPSERT).\n
- Pratik doğrulama: `core.fixtures` içinde belirli bir `id` için tek satır olduğundan emin olun.

---

## 5) Incident runbooks

### 5.1 Quota low / emergency stop
- Belirti: `daily_remaining` hızla düşer veya collector loglarında `emergency_stop_daily_quota_low`.
- Aksiyon:
  - Backfill ve ağır job’ları disable et (config-driven).
  - Sadece temel daily job’lar (fixtures/standings/injuries) açık kalsın.
  - `get_raw_error_summary()` ile 4xx/429 trendini kontrol et.

### 5.2 Per-minute rateLimit (429 / errors.rateLimit)
- Belirti: collector loglarında `api_errors:/teams:{rateLimit: ...}` veya `api_rate_limited`.
- Kalıcı önlemler (mevcut sistem):
  - Token bucket startup burst engeli (bucket default 0 token ile başlar).
  - `/teams` dependency cache: `core.team_bootstrap_progress` (completed=true ise aynı league+season için `/teams` tekrar çağrılmaz).
- Aksiyon (acil):
  - Backfill hızını düşür:
    - `BACKFILL_FIXTURES_MAX_TASKS_PER_RUN`
    - `BACKFILL_FIXTURES_MAX_PAGES_PER_TASK`
    - `BACKFILL_FIXTURES_WINDOW_DAYS`
  - MCP: `get_raw_error_summary(since_minutes=60)` ile 429 trendini doğrula.

### 5.4 MCP 4xx/5xx (özellikle 406/400/504)
- **406 Not Acceptable**: client `Accept` header’ında hem `application/json` hem `text/event-stream` sunmuyor.
- **400 Missing session ID**: `mcp-session-id` header’ı yok (stateful).
- **-32602 Invalid request parameters**: `tools/list` için `params` object değil → `{"cursor": null}` gönder.
- **504 Gateway Timeout**: reverse proxy upstream’e ulaşamıyor (routing/network). Prod’da Traefik path davranışı nedeniyle endpoint doğru olmalı: `/mcp`.

### 5.5 Postgres log gürültüsü: `invalid startup packet` / `unsupported frontend protocol`
- Belirti: Postgres loglarında şu tip mesajlar:
  - `invalid length of startup packet`
  - `incomplete startup packet`
  - `unsupported frontend protocol ...`
  - `no PostgreSQL user name specified in startup packet`
- Kök neden (en sık): **5432 portu public expose** edilmiş ve internetten random scanner/healthcheck’ler Postgres’e HTTP/garbage gönderiyor.
- Risk:
  - Güvenlik yüzeyi büyür (DB brute-force / scan)
  - Log gürültüsü gerçek incident’leri gömer
- Aksiyon:
  - Prod’da **Postgres portunu public’e açma**:
    - Coolify/Traefik: DB service’i sadece internal network’te kalsın
    - Eğer host port mapping varsa kaldır / firewall ile sadece allowlist IP’lere aç
  - DB erişimi ops/backup için gerekiyorsa:
    - VPN üzerinden erişim veya SSH tunnel kullan
    - DB user’ı minimum yetkilerle sınırla

### 5.6 Postgres loglarında `docker: command not found` / `systemctl: command not found`
- Not: **Resmi `postgres:*` image** normal şartlarda `docker`, `systemctl`, `service`, `setenforce` çalıştırmaz.
- Kök neden (muhtemel):
  - Coolify’de Postgres servisi yanlış image/entrypoint ile çalışıyor (bash script wrapper)
  - Repo dışı init/sidecar script’i container içinde koşuyor
- Aksiyon:
  - Coolify’de **postgres service tanımı** kontrol et:
    - Image: `postgres:15-alpine` (veya sizin seçtiğiniz resmi postgres)
    - Custom command/entrypoint: boş olmalı (default entrypoint)
    - Volume: sadece Postgres data + opsiyonel `/docker-entrypoint-initdb.d` (SQL) olmalı
  - Bu satırlar devam ederse: ilgili container’ın gerçekten Postgres container’ı olduğunu doğrula (log kaynağı karışmış olabilir).

### 5.2 RAW var CORE yok (transform/upsert sorunu)
- Belirti: `raw.api_responses` artıyor ama `core.fixtures`/diğer tablolar artmıyor.
- Aksiyon:
  - Collector loglarında `db_upsert_failed` / transform hataları.
  - DB constraint/FK ihlalleri için Postgres logs.
  - FK bağımlılıkları: leagues/teams/venues → fixtures sırası.

### 5.7 Standings backfill `missing_teams_in_core:*`
- Belirti:
  - MCP `get_backfill_progress()` içinde standings task’larında `last_error=missing_teams_in_core:N`
  - Bu, `core.standings` FK koruması yüzünden “crash” yerine **skip+log** davranışıdır.
- Kök neden (tipik):
  - `/standings` response içindeki bazı `team_id`’ler `/teams?league&season` sonucunda gelmeyebilir.
- Çözüm (mevcut sistem):
  - Dependencies katmanı eksik takımlar için **fallback** uygular: `GET /teams?id=<team_id>` ve CORE `teams/venues` upsert.
  - Bir sonraki standings backfill çalıştırmasında task otomatik tamamlanır.

### 5.3 Coverage düşmüş
- Belirti: `mart.coverage_status.overall_coverage` düşer.
- Aksiyon:
  - MCP `get_coverage_status(league_id)` ile endpoint bazında bak.
  - İlgili endpoint için `get_last_sync_time()` ve RAW error summary.

### 5.8 “Canlı gibi takılı kalan” maçlar (stale live status)

Hibrit yaklaşım (auto-finish + verification + stale_refresh):

**Aşama 1: Auto-finish (DB-only, API çağrısı yok)**
- Belirti:
  - Claude/MCP canlı taramasında “stale” görünen fixtures (örn. `1H/2H/HT/INT/SUSP`) ve `updated_at` çok eski.
  - MCP: `stale_fixtures_report(threshold_hours=2, safety_lag_hours=3)` → `stale_count>0`
- Otomatik çözüm:
  - `auto_finish_stale_fixtures` job’ı DB’de direkt status’u `FT`'ye çevirir.
  - **Yeni özellik (v3.1)**: `try_fetch_first=true` ise önce API’den batch fetch dener, başarısız olursa DB-only update yapar.
  - Güvenlik: Double-threshold kontrolü (`date < now-2h` VE `updated_at < now-3h`).
  - SQL bug fix (v3.1): `league_id` WHERE clause eklendi, parametre sırası düzeltildi.
  - Scope: `config/jobs/daily.yaml -> tracked_leagues`.
  - Verification flag: Auto-finished fixture’lara `needs_score_verification = TRUE` set edilir (API fetch başarısızsa).
- Aksiyon:
  - `auto_finish_stats(hours=24)` ile kaç maç auto-finished’i kontrol et.
  - Beklenen: Her saat başı 50-500 arası maç FT’ye geçmeli.
  - Log’larda `auto_finish_complete` mesajında `fetched_from_api` ve `marked_for_verification` sayıları görünür.

**Aşama 1.5: Verification job (auto-finished maçların skorlarını doğrulama)**
- Belirti:
  - Auto-finished maçların skorları yanlış olabilir (API bağlantısı kesildiğinde eski skorla kapatılmış).
  - MCP: `get_job_status(job_name="auto_finish_verification")` → job çalışıyor mu?
- Otomatik çözüm:
  - `auto_finish_verification` job’ı `needs_score_verification = TRUE` olan fixture’ları seçer.
  - Batch fetch: `GET /fixtures?ids=...` (max 20 per request).
  - UPSERT CORE ile güncel skorları yazar, `needs_score_verification = FALSE` yapar.
  - Quota guard: Sadece `daily_remaining >= min_daily_quota` (default: 50000) olduğunda çalışır.
  - Sıklık: Her 30 dakikada bir (cron `*/30 * * * *`).
- Aksiyon:
  - Log’larda `auto_finish_verification_complete` mesajını kontrol et.
  - `fixtures_verified` sayısı zamanla artmalı.
  - Quota düşükse `auto_finish_verification_quota_guard` mesajı görünür (normal).

**Aşama 2: Stale refresh (API çağrılı, kalan durumlar için)**
- Belirti:
  - Auto-finish’e girmeyen ama yine de stale durumdaki maçlar (örn. 1-2 saat önce güncellenmiş).
  - MCP: `get_stale_live_fixtures_status(threshold_minutes=15, tracked_only=true, scope_source="daily")` → `stale_count>0`
- Otomatik çözüm:
  - `stale_live_refresh` job’ı bu fixture id’lerini seçer ve `/fixtures?ids=...` ile tekrar çekip CORE’daki status’ü düzeltir.
  - Scope: `scope_source="daily"` → `config/jobs/daily.yaml -> tracked_leagues`.
- Aksiyon:
  - `get_job_status(job_name="stale_live_refresh")` ile job loglarını doğrula.
  - Eğer `stale_count` uzun süre düşmüyorsa:
    - Quota / 429 var mı kontrol et: `get_raw_error_summary(since_minutes=60)`
    - DB’de fixture `updated_at` ilerliyor mu kontrol et.

**Doğrulama:**
- Auto-finish sonrası: `stale_fixtures_report()` → `stale_count` zamanla 0’a yaklaşmalı.
- Verification sonrası: `get_job_status(job_name="auto_finish_verification")` → `last_event` güncel olmalı.
- Stale refresh sonrası: `get_stale_live_fixtures_status()` → `stale_count` çok düşük kalmalı (<10).

**Manuel düzeltme (one-time script):**
- Geçmiş auto-finished maçları düzeltmek için:
  - `python scripts/fix_auto_finished_scores.py [--dry-run] [--date-from YYYY-MM-DD]`
  - Batch fetch ile skorları karşılaştırır ve düzeltir.

---

## 6) 83 Lig Rollout (Wave planı) + 30–60 dk gözlem

Bu bölüm, wave1/wave2 rollout’u **config-driven** yapar ve MCP ile gözlemi tanımlar.

### 6.1 Wave uygulama

Dalga seçmek için helper:
- `scripts/apply_league_wave.py`

Örnek:
- Wave1 (ilk 10 lig):

```bash
python3 scripts/apply_league_wave.py --size 10
```

- Wave2 (+25 lig):

```bash
python3 scripts/apply_league_wave.py --size 25 --offset 10
```

Uygulama sonrası:
- Coolify redeploy (collector)
- MCP üzerinden gözlem

### 6.2 MCP ile gözlem (30–60 dk)

Minimum:
- `get_backfill_progress()` → satır sayısı ve completed oranı artmalı
- `get_raw_error_summary(since_minutes=60)` → rateLimit/5xx yükselmiyorsa OK
- `get_rate_limit_status()` → minute_remaining trendi stabil
- `get_database_stats()` → core.fixtures/core.teams artışı

### 6.3 DB gözlemi (ops)

```sql
SELECT completed, COUNT(*) FROM core.team_bootstrap_progress GROUP BY completed;

SELECT job_id, COUNT(*) AS total, SUM(CASE WHEN completed THEN 1 ELSE 0 END) AS completed
FROM core.backfill_progress
GROUP BY job_id
ORDER BY job_id;
```

