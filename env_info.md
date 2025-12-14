# Coolify Environment Variables — Detailed Reference (Production v3.0)

Bu doküman, Coolify üzerinde tanımladığımız ENV değişkenlerinin **ne yaptığını**, **hangi servisleri etkilediğini**, **varsayılanlarını**, ve **yanlış ayarda oluşabilecek sonuçları** açıklar.

Kaynaklar:
- Compose: `docker-compose.yml`
- Config: `config/api.yaml`, `config/rate_limiter.yaml`, `config/jobs/*.yaml`
- Kod: `src/collector/*`, `src/jobs/*`, `src/mcp/*`, `scripts/healthcheck_*.py`

---

## 1) Domain / URL (Coolify otomatik ENV’leri)

Bu değişkenler Coolify’nin domain routing / reverse-proxy katmanında kullanılır. Uygulama kodu genelde doğrudan okumaz; Coolify servis URL’lerini ve TLS’yi yönetir.

- **`SERVICE_FQDN_COLLECTOR`**
  - **Ne**: Collector servisi için FQDN (ör: `ogz.zinalyze.pro`)
  - **Etkiler**: Coolify routing. Collector genelde dış dünyaya HTTP sunmaz; bu değer daha çok Coolify UI/Proxy tarafında görünür.

- **`SERVICE_URL_COLLECTOR`**
  - **Ne**: Collector için tam URL (ör: `https://ogz.zinalyze.pro`)
  - **Etkiler**: Coolify routing/health (Coolify ayarlarına göre).

- **`SERVICE_FQDN_LIVE_LOOP`**, **`SERVICE_URL_LIVE_LOOP`**
  - **Ne**: Live loop servisi domain/URL
  - **Etkiler**: Live loop HTTP sunmadığı için pratikte routing gerekmeyebilir; Coolify proje şablonları otomatik ekleyebilir.
  - **Not**: Bizde `ENABLE_LIVE_LOOP=0` ile live loop **kapalı** (API çağrısı yapmaz).

- **`SERVICE_FQDN_MCP`**, **`SERVICE_URL_MCP`**
  - **Ne**: MCP servisi için FQDN/URL (ör: `mcp.zinalyze.pro`)
  - **Etkiler**: MCP, HTTP/SSE üzerinden dışarı açıldığı için bu alanlar **aktif** kullanılır (reverse-proxy → container port mapping).

---

## 2) API-Football erişimi (zorunlu)

- **`API_FOOTBALL_KEY`** (**zorunlu**)
  - **Ne**: API-Football v3 API key.
  - **Nerede kullanılır**: `src/collector/api_client.py`
  - **Nasıl kullanılır**: Her API isteğinde **tek header** olarak `x-apisports-key` set edilir.
  - **Yanlışsa**: 401/403 benzeri cevaplar; RAW’da `errors` alanları dolar; coverage ilerlemez.
  - **Güvenlik**: Koda hard-code edilmez; sadece ENV.

---

## 3) PostgreSQL bağlantısı (zorunlu)

Sistem, DB bağlantısını öncelikle `DATABASE_URL` ile bekler. Compose ayrıca uyumluluk için `POSTGRES_*` değişkenlerini de taşır.

- **`DATABASE_URL`** (**önerilen / pratikte zorunlu**)
  - **Örnek**: `postgresql://postgres:postgres@postgres:5432/api_football`
  - **Etkiler**:
    - Collector RAW/CORE/MART yazımı
    - MCP read-only sorgular
    - Healthcheck script’leri
  - **Yanlışsa**: Servisler ayakta kalsa bile DB bağlantısı kurulamaz; job’lar hata verir.

- **`POSTGRES_HOST`**, **`POSTGRES_PORT`**, **`POSTGRES_USER`**, **`POSTGRES_PASSWORD`**, **`POSTGRES_DB`**
  - **Ne**: DB parçalı tanımı.
  - **Etkiler**: Bazı script/utility’ler fallback olarak bunları kullanabilir.
  - **Öneri**: Coolify’de tek kaynak olarak `DATABASE_URL` kullan; diğerlerini uyumluluk için tut.

---

## 4) Scheduler / zaman dilimi

- **`SCHEDULER_TIMEZONE`** (önerilen)
  - **Örnek**: `Europe/Istanbul`
  - **Nerede kullanılır**: `src/collector/scheduler.py` (cron trigger timezone)
  - **Etkisi**:
    - Cron schedule’ların hangi timezone’a göre çalışacağı.
    - **DB’ye yazılan timestamp’ler yine UTC**; sadece job tetikleme yerel timezone.
  - **Yanlış ayar sonucu**: Job’lar beklenenden farklı saatlerde çalışır (özellikle daily standings).

---

## 5) Live loop kontrolü (bizde kapalı)

- **`ENABLE_LIVE_LOOP`**
  - **Değerler**: `0` / `1`
  - **Nerede kullanılır**: `docker-compose.yml` live_loop `command` bloğu.
  - **Etkisi**:
    - `0`: container idle kalır, **API çağrısı yok**
    - `1`: `scripts/live_loop.py --interval 15` çalışır (`/fixtures?live=all`)
  - **Not (quota)**: Live loop açılırsa dakikada ~4 request (15s interval) ek yük gelir; minute limit’e yaklaşırken dikkat.

- **`REDIS_URL`**
  - **Örnek**: `redis://redis:6379/0`
  - **Nerede kullanılır**: live loop state/delta (repo içindeki live loop tasarımına bağlı)
  - **Etkisi**: Live loop açıkken stabil delta/tekrarsız işleme için gereklidir.
  - **Bizde**: Live loop kapalı olsa bile env’de durabilir.

---

## 6) Backfill throughput kontrolü (quota-safe hız ayarı)

Bu değişkenler backfill’i **kota-safe** biçimde hızlandırmak/slowlamak için vardır. Rate limiter yine global güvenlik katmanıdır; ancak bu ENV’ler “iş seçimi” ve “batch büyüklüğü” üzerinden **iş hacmini** kontrol eder.

### 6.1 Fixtures backfill (league+season windowing)

- **`BACKFILL_FIXTURES_MAX_TASKS_PER_RUN`**
  - **Ne**: Her scheduler çalıştırmasında kaç `(league, season)` görevi alınacağını sınırlar.
  - **Etkisi**: Büyük değer = daha hızlı backfill, daha çok DB yazımı, daha çok API request.

- **`BACKFILL_FIXTURES_MAX_PAGES_PER_TASK`**
  - **Ne**: Her `(league, season)` için kaç **window** işlenecek.
  - **Not**: ENV adı geçmişte “page” idi; şimdi **window sayısı** için aynı isim korunuyor (Coolify env compatibility).
  - **Etkisi**: Büyük değer = aynı task’ta daha çok tarih aralığı işlenir.

- **`BACKFILL_FIXTURES_WINDOW_DAYS`**
  - **Ne**: Her window kaç günlük aralık kapsasın (default: 30).
  - **Etkisi**:
    - Küçük değer: daha fazla window → daha fazla request → daha granular progress
    - Büyük değer: daha az window → daha az request → tek seferde daha çok fixture upsert (DB yükü artabilir)

### 6.2 Standings backfill

- **`BACKFILL_STANDINGS_MAX_TASKS_PER_RUN`**
  - **Ne**: Her koşuda kaç `(league, season)` standings işi yapılacak.
  - **Etkisi**: Standings “ucuz” (1 request/task) olduğu için genelde küçük tutmak yeterli.

---

## 7) Fixture details throughput kontrolü (per-fixture 4 endpoint)

Fixture details job’ları, her fixture için 4 endpoint çağırır:
`/fixtures/players`, `/fixtures/events`, `/fixtures/statistics`, `/fixtures/lineups`.

- **`FIXTURE_DETAILS_FINALIZE_BATCH`**
  - **Ne**: Recent finalize’da (son 24h biten maçlar) kaç fixture işlenecek.
  - **Etkisi**: Büyük değer = daha hızlı finalize, daha yüksek request burst (4x batch).

- **`FIXTURE_LINEUPS_WINDOW_BATCH`**
  - **Ne**: Kickoff window içinde lineups gecikmesini yakalamak için kaç fixture “lineups-only” denenecek.
  - **Etkisi**: Büyük değer = lineups freshness artar, daha çok request.

- **`FIXTURE_DETAILS_BACKFILL_BATCH`**
  - **Ne**: 90 günlük backfill’de her koşuda kaç fixture alınacak.
  - **Etkisi**: Büyük değer = daha hızlı backfill, daha çok request (4x batch).
  - **Not**: Bu job config’de disabled olabilir; enable edilirse bu batch kritik hale gelir.

---

## 8) Venues backfill / dependency kontrolü

- **`VENUES_BACKFILL_MAX_PER_RUN`**
  - **Ne**: Venue backfill helper’ının bir koşuda en fazla kaç venue çekebileceğini sınırlar.
  - **Etkisi**: Venue eksikleri hızlı kapanır ama ekstra API çağrısı artar.
  - **Not**: Ayrıca fixtures dependency aşamasında fixtures envelope içinden venue UPSERT yapıldığı için FK hataları önlenir; bu ENV daha çok “missing venue details” kapanışı içindir.

---

## 9) MCP (HTTP/SSE) ayarları — Coolify için kritik

MCP servisi Coolify’da ayrı service olarak çalışır. Transport genelde **SSE**’dir.

- **`MCP_TRANSPORT`**
  - **Değerler**: `sse` (Coolify/HTTP), `stdio` (Claude Desktop local), (opsiyonel: `streamable-http` repo implementasyonuna bağlı)
  - **Etkisi**: MCP server’ın hangi protokolde çalışacağını belirler.

- **`MCP_MOUNT_PATH`**
  - **Ne**: MCP route’larının mount prefix’i.
  - **Default**: boş → `/`
  - **Etkisi**: Reverse-proxy altında path bazlı yayın yapılacaksa kullanılır.

- **`FASTMCP_HOST`**
  - **Default**: `0.0.0.0`
  - **Etkisi**: Container içinde hangi interface’e bind edileceği. Coolify için `0.0.0.0` gerekir.

- **`FASTMCP_PORT`**
  - **Default**: `8000`
  - **Etkisi**: Container içindeki MCP listen port’u.

- **`MCP_HOST_PORT`**
  - **Default**: `8001`
  - **Neden var**: Host’ta 8000 başka servis tarafından kullanılıyor olabilir.
  - **Etkisi**: Compose port mapping: `${MCP_HOST_PORT}:${FASTMCP_PORT}`.
  - **Yanlışsa**: Deploy sırasında `port is already allocated` hatası alırsın.

- **`FASTMCP_LOG_LEVEL`**
  - **Default**: `INFO`
  - **Etkisi**: MCP log verbosity.

- **`COLLECTOR_LOG_FILE`** (MCP service içinde)
  - **Default**: `/app/logs/collector.jsonl`
  - **Etkisi**: MCP, bazı tool’larda “job status / last sync” gibi gözlemler için collector logunu okur.

---

## 10) Rate limit / quota ayarları nerede?

Önemli ayrım:
- **Limitler ENV ile değil**, YAML config ile yönetilir:
  - `config/api.yaml` (daily limit bilgi amaçlı)
  - `config/rate_limiter.yaml` (**minute_soft_limit**, **daily_limit**, **emergency_stop_threshold**)
- Coolify ENV’leri ise daha çok **iş hacmi (batch/task)** ve **deploy/runtime** davranışını kontrol eder.

Bu sayede:
- Üretim limiti değişince kodu değil config’i değiştirirsin.
- Backfill hızını, quota-safe olarak ENV üzerinden ayarlarsın.


