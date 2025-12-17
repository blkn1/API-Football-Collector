## Production Runbook (v3)

Bu runbook; çalışan collector + Postgres + MCP + Read API stack’inde **deploy sonrası doğrulama**, **günlük operasyon**, ve **incident** (quota/coverage/data gap) akışlarını tek yerde toplar.

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
  - Beklenen minimum: liste, alanlar: `id, league_id, date_utc, status, home_team, away_team`

Ek ops gözlemler (Phase 1.5):
- **Backfill progress**: `get_backfill_progress()`
- **RAW error health**: `get_raw_error_summary(since_minutes=60)`
- **Recent error logs**: `get_recent_log_errors(limit=50)`

### 1.3 PASS / FAIL kriteri
- **PASS**: A+B+D `ok=true` ve DB bağlantısı sağlıklı.
- **FAIL**: Tool exception / DB bağlantı hatası / output şeması bozulmuş.

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

### 3.2 SSE (read-only)
- `GET /v1/sse/system-status` → event: `system_status`
- `GET /v1/sse/live-scores` → event: `live_score_update`

### 3.3 Access control (prod)
- Basic auth:
  - `READ_API_BASIC_USER`
  - `READ_API_BASIC_PASSWORD`
- IP allowlist (ops):
  - `READ_API_IP_ALLOWLIST` (comma-separated)

---

## 4) Günlük operasyon checklist
- **Quota trend**: `get_rate_limit_status()` / `GET /v1/quota`
- **Coverage**: `get_coverage_summary()` + en düşük coverage satırlarını `get_coverage_status(league_id)` ile drilldown
- **Job health**: `get_job_status()` + `get_recent_log_errors()`
- **Data drift**: `get_raw_error_summary(since_minutes=1440)` (son 24h)

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

