## MCP Usage Guide (API-Football Collector)

Bu doküman, `api-football` projesinin **MCP (Model Context Protocol)** sunucusunu nasıl çalıştıracağını ve **Claude Desktop** üzerinden nasıl bağlanacağını anlatır.

> MCP bu projede **read-only gözlem/izleme** katmanıdır. DB üzerinde INSERT/UPDATE yapmaz.

---

## 1) MCP Nedir? Bu projede ne işe yarar?

MCP sunucusu (`src/mcp/server.py`), PostgreSQL’deki RAW/CORE/MART katmanlarından **read-only** sorgular yapıp tool çıktıları üretir.

Temel kullanım:
- Quota (daily/minute remaining)
- DB sayımları (RAW/CORE)
- Coverage özetleri
- Job durumları (config + collector log tail)
- Backfill progress
- RAW hata özetleri

> Not: Bu projede “tek bakışta sistem” için ayrıca Read API içinde `/ops` dashboard vardır.
> MCP ise daha geniş kapsamlı tool seti sunar.

---

## 1.1 Ops Panel (Read API) ile “tek bakışta sistem”

Read API servisinde (Basic Auth/IP allowlist korumalı) ops dashboard:
- `GET /ops` (HTML dashboard)
- `GET /ops/api/system_status` (JSON; dashboard bunu poll eder)

`/ops/api/system_status` tek response içinde şu gözlemleri taşır:
- **quota** → MCP: `get_rate_limit_status()`
- **db** → MCP: `get_database_stats()`
- **coverage_summary** → MCP: `get_coverage_summary(season=...)`
- **job_status** → MCP: `get_job_status()`
- **backfill** → MCP: `get_backfill_progress()`
- **raw_errors** → MCP: `get_raw_error_summary()`
- **raw_error_samples** → MCP: `get_raw_error_samples()`
- **recent_log_errors** → MCP: `get_recent_log_errors()`

Kısıt:
- `/ops` bir “dashboard”tır; MCP’deki tüm tool’ları tek tek expose etmez.
- Prod’da “tam detay” için MCP tool’larını doğrudan çağırın (aşağıdaki bölümler).

---

## 2) Transport modları: Claude Desktop vs Prod

### 2.1 Claude Desktop (Local) → **stdio**
Claude Desktop tipik olarak MCP’yi **stdio** üzerinden çalıştırır.
- `MCP_TRANSPORT=stdio`
- Claude Desktop, komutu local’de çalıştırır ve tool çağrıları stdio’dan gider.

### 2.2 Coolify/Prod → **streamable-http** (Traefik)
Prod’da MCP servisiniz HTTP üzerinden **streamable-http** çalışır.
- `MCP_TRANSPORT=streamable-http`
- `MCP_MOUNT_PATH=/mcp` (prod endpoint)
- Reverse proxy: **Traefik** (Coolify)

> Not: Streamable HTTP MCP **stateful** çalışır. İstemci (client) `Accept` header’ında hem `application/json` hem `text/event-stream` desteklediğini belirtmeli ve **session id + initialize** akışını takip etmelidir.

---

## 3) Claude Desktop kurulumu (Linux)

### 3.1 Gerekenler
- Bu repo local’de mevcut olmalı (ör: `/home/ybc/Desktop/api-football`)
- Python 3 kurulu olmalı
- Proje bağımlılıkları kurulu olmalı (`requirements.txt`)

### 3.2 Claude Desktop MCP config dosyası
Linux’ta tipik path:
- `~/.config/Claude/claude_desktop_config.json`

Aşağıdaki config ile MCP sunucusunu Claude Desktop başlatır.

> Önemli: `cd` gerektirdiği için komutu `bash -lc` ile çalıştırıyoruz.

```json
{
  "mcpServers": {
    "api-football": {
      "command": "bash",
      "args": [
        "-lc",
        "cd /home/ybc/Desktop/api-football && MCP_TRANSPORT=stdio DATABASE_URL='<PASTE_DATABASE_URL>' COLLECTOR_LOG_FILE='/home/ybc/Desktop/api-football/logs/collector.jsonl' python3 -m src.mcp.server"
      ]
    }
  }
}
```

---

## 4) Claude Desktop → Prod MCP (remote) (streamable-http)

Claude Desktop doğrudan HTTP transport’ları konuşmadığı için **stdio→streamable-http adapter** kullanılır.

Örnek:

```json
{
  "mcpServers": {
    "api-football": {
      "command": "npx",
      "args": [
        "-y",
        "@pyroprompts/mcp-stdio-to-streamable-http-adapter"
      ],
      "env": {
        "URI": "https://mcp.zinalyze.pro/mcp"
      },
      "timeout": 30000,
      "initTimeout": 20000
    }
  }
}
```

Notlar:
- Bu adapter, streamable-http’in **session + initialize** akışını otomatik yönetir.
- Eğer Claude’da `Tool '...:get_backfill_progress' not found` görürsen, bu genelde **yanlış proxy/yanlış endpoint** demektir. Bu bölümdeki adapter config’e geçince düzelir.

### 3.3 ENV açıklamaları
- **`MCP_TRANSPORT=stdio`**: Claude Desktop için.
- **`DATABASE_URL`**: MCP’nin okuyacağı Postgres.
- **`COLLECTOR_LOG_FILE`**: `get_job_status()` ve `get_recent_log_errors()` için JSONL log path.
- Opsiyonel:
  - `API_FOOTBALL_DAILY_CONFIG`: MCP’nin season/tracked leagues okuduğu config (default: `config/jobs/daily.yaml`).
  - `MCP_LOG_TAIL_LINES`: log tail okuma limiti (default 2000/4000).

---

## 5) Prod streamable-http hızlı doğrulama (opsiyonel)

Örnek domain: `https://mcp.zinalyze.pro`

### 5.1 Session + initialize + tools/list (curl)

Streamable HTTP için doğru akış:

1) **initialize** (server response header’ında `mcp-session-id` döner; onu kopyala)

```bash
curl -i -X POST "https://mcp.zinalyze.pro/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  --data '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0.1"}}}'
```

2) **tools/list** (aynı session id ile)

```bash
curl -i -X POST "https://mcp.zinalyze.pro/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: <PASTE_SESSION_ID>" \
  --data '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"cursor":null}}'
```

Sık hata mesajları:
- `406 Not Acceptable`: `Accept` header’ında iki tip de yok.
- `400 Missing session ID`: `mcp-session-id` header’ı yok.
- `-32602 Invalid request parameters`: `tools/list` için `params` object değil (örn. `{ "cursor": null }` kullan).

### 5.1.1 “MCP’de hangi tool’lar var?” (tam liste)
Üretimde MCP’ye eklediğimiz her yeni izleme/test aracı **tools/list** ile görünür.
En hızlı yol:
- `bash scripts/smoke_mcp.sh` (initialize → tools/list → örnek tools/call)

> Bu liste “MCP ile çağırabileceğimiz her şey”dir.

### 5.1.2 tools/call örneği (curl)
Örnek: `get_database_stats()` çağırmak:

```bash
curl -sS -X POST "https://mcp.zinalyze.pro/mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: <PASTE_SESSION_ID>" \
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_database_stats","arguments":{}}}'
```

### 5.2 Tam smoke test (initialize → tools/list → tools/call) (script)
Prod’da “MCP bazen çalışıyor, redeploy sonrası bozuluyor” gibi durumlarda en hızlı teşhis, stateful session akışını baştan sona test etmektir.

Repo script’i:

```bash
bash scripts/smoke_mcp.sh
```

Varsayılan olarak `SERVICE_URL_MCP` (Coolify env) varsa onu kullanır; yoksa `https://mcp.zinalyze.pro` kullanır.
Gerekirse override:

```bash
MCP_BASE_URL="https://mcp.zinalyze.pro" MCP_PATH="/mcp" bash scripts/smoke_mcp.sh
```

Notlar:
- **Redeploy sonrası** MCP server restart olur → **eski `mcp-session-id` geçersiz** olur. Script’i yeniden çalıştır.
- `streamable-http` için request’lerde `Accept: application/json, text/event-stream` zorunlu.

---

## 6) MCP Tool kataloğu (bu projede)

Tool’lar `src/mcp/server.py` içinde `@app.tool()` ile tanımlıdır.

### 5.1 Sistem/operasyon
- `get_rate_limit_status()`
  - RAW header’larından daily/minute remaining.
- `get_database_stats()`
  - RAW/CORE tablo sayımları, son aktivite.
  - Not: `core_top_scorers` ve `core_team_statistics` sayımları da dahildir.
- `get_job_status(job_name=None)`
  - Job config + collector log tail merge.
  - Not: Eğer log event yoksa, MCP RAW tablodan “son görülen fetch” kanıtını ekler:
    - `last_seen_at_utc`, `last_seen_source=raw`, `last_raw_fetched_at_utc`
- `get_coverage_summary(season=None)`
- `get_coverage_status(league_id=None, season=None)`

### 5.2 Backfill + hata gözlemi
- `get_backfill_progress(job_id=None, season=None, include_completed=False, limit=200)`
- `get_standings_refresh_progress(job_id="daily_standings")`
  - daily_standings “parça parça” çalışıyorsa cursor/total_pairs/last_run_at gösterir.
- `get_raw_error_summary(since_minutes=60, endpoint=None, top_endpoints_limit=25)`
- `get_raw_error_samples(since_minutes=60, endpoint=None, limit=25)`
- `get_recent_log_errors(job_name=None, limit=50)`

### 5.4 Live loop (legacy)
Bu deployment’ta live polling **yok** (compose’ta `live_loop` servisi kaldırıldı).
- `get_live_loop_status(since_minutes=5)` tool’u **legacy** olarak durabilir ama beklenen çıktı genelde `running=false` olur.

### 5.4.1 Stale live fixtures (ops maintenance gözlemi)
- `get_stale_live_fixtures_status(threshold_minutes=30, tracked_only=true, scope_source="daily")`
  - Amaç: CORE’da “canlı gibi kalan ama güncellenmeyen” maçları tespit etmek.
  - `tracked_only=true` iken scope seçimi:
    - `scope_source="daily"` → `config/jobs/daily.yaml -> tracked_leagues`
  - Not: Bu deployment’ta `live.yaml`/live loop devre dışıdır; bu yüzden `scope_source="daily"` kullanın.

### 5.5 Daily fixtures cadence gözlemi (30dk job gerçekten çalışıyor mu?)
- `get_daily_fixtures_by_date_status(since_minutes=180)`
  - `/fixtures?date=YYYY-MM-DD` çağrılarının RAW’a düştüğünü doğrular.
  - `get_job_status()` log parse hatalarından bağımsızdır; doğrudan RAW üzerinden kanıt verir.

### 5.3 Veri sorguları
- `query_fixtures(league_id=None, date=None, status=None, limit=10)`
- `query_standings(league_id, season)`
- `query_teams(league_id=None, search=None, limit=20)`
- `query_injuries(league_id=None, season=None, team_id=None, player_id=None, limit=50)`
- `get_fixture_detail_status(fixture_id)`
- `query_fixture_players(fixture_id, team_id=None, limit=300)`
- `query_fixture_events(fixture_id, limit=300)`
- `query_fixture_statistics(fixture_id)`
- `query_fixture_lineups(fixture_id)`

---

## 6.1 “Yeni eklenen dataset’ler doluyor mu?” (top_scorers / team_statistics)

MCP ile doğrulama:
- DB sayımı: `get_database_stats()` → `core_top_scorers`, `core_team_statistics`
- Coverage drilldown: `get_coverage_status(league_id=<LID>, season=<SEASON>)` içinde
  - `/players/topscorers`
  - `/teams/statistics`

DB kanıtı (postgres terminal):
- `SELECT COUNT(*) FROM core.top_scorers;`
- `SELECT COUNT(*) FROM core.team_statistics;`

Cron’u beklemeden doğrulama (collector terminal):
- `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=top_scorers_daily python3 scripts/run_job_once.py`
- `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=team_statistics_refresh python3 scripts/run_job_once.py`

> Not: MCP katmanı read-only’dur; job tetikleme MCP üzerinden yapılmaz (bilinçli güvenlik kararı).

---

## 7) Claude Desktop test senaryosu (minimum acceptance)

Claude’a şu sırayla tool çağırmasını söyle (prod ops “minimum + genişletilmiş”):

### 7.1 Minimum (deploy sonrası smoke / acceptance)
1) `get_database_stats()`
   - DB bağlantısı OK mi?
   - RAW/CORE sayımlar geliyor mu?
2) `get_rate_limit_status()`
   - `daily_remaining` geliyor mu?
   - `minute_remaining` None olabilir (header her zaman gelmeyebilir) → bu tek başına FAIL değildir.
3) `get_raw_error_summary(since_minutes=60)`
   - Son 60 dk: `err_4xx/err_5xx/envelope_errors` artıyor mu?
4) `get_backfill_progress()`
   - `pending_tasks` düşüyor mu? (backfill bitmişse 0 kalır)

### 7.2 Prod “sürekli çalışma” doğrulamaları (live + daily cadence)
5) (legacy) `get_live_loop_status(since_minutes=5)`
   - Beklenen: bu deployment’ta `running=false`
5.1) `get_stale_live_fixtures_status(threshold_minutes=30, tracked_only=true, scope_source="daily")`
   - Beklenen: normalde `stale_count=0` (tracked/live scope içinde).
   - Eğer `stale_count>0` ise bu “takılı canlı statü” işaretidir; stale_live_refresh job’ı takip edilmelidir.
6) `get_daily_fixtures_by_date_status(since_minutes=180)`
   - Beklenen:
     - `daily_fixtures_by_date` cron’u çalışıyorsa `running=true` ve `last_fetched_at_utc` son 24h içinde güncellenir.
   - Not: Bu tool log parse’a dayanmaz; doğrudan RAW’dan kanıtlar.
   - Not: `fixtures_fetch_mode=per_tracked_leagues` iken `global_requests/pages_fetched/max_page/results_sum` genelde 0/None olur (normal).
7) `get_last_sync_time(endpoint="/fixtures")`
   - Beklenen: `/fixtures` için son fetch timestamp’ı güncel.

### 7.3 Job gözlemi (opsiyonel ama önerilir)
8) `get_job_status()`
   - Job listesi ve son event’ler (best-effort).
   - Not: Log formatı/volume’a göre bazı job’lar “unknown” görünebilir; bu durumda 7.2’deki RAW tabanlı tool’lar esas alınır.

### 7.4 Coverage gözlemi (opsiyonel)
9) `get_coverage_summary(season=<CURRENT>)`
10) `get_coverage_status(league_id=<LID>, season=<CURRENT>)`
   - Beklenen: overall coverage yüksek; `lag_minutes` scheduler cadence ile uyumlu.
   - Not: `get_coverage_*` tool’ları **varsayılan olarak tracked leagues** ( `config/jobs/daily.yaml -> tracked_leagues` ) ile sınırlıdır.
     Tüm ligleri görmek istersen: `tracked_only=false`

PASS kriteri:
- 7.1 minimum set’te tool’lar exception üretmeden dönüyor (`ok=true`).
- `get_raw_error_summary()` içinde 4xx/5xx/envelope_errors anormal yükselmiyor.
- Daily fixtures cron ayarlı ise `get_daily_fixtures_by_date_status().running=true`.

FAIL kriteri:
- `get_database_stats()` DB error / exception
- `get_raw_error_summary()` 429/5xx/envelope_errors yükseliyor
- Daily fixtures cron ayarlı olmasına rağmen `get_daily_fixtures_by_date_status().running=false` (scheduler veya config sorunu)

---

## 8) Sık görülen sorunlar

### 7.1 `season_required`
`get_coverage_*` veya bazı filtreli tool’lar season ister.
- Çözüm: `config/jobs/daily.yaml` içinde `season:` olmalı veya tool’a `season=...` parametresi verilmeli.

### 7.2 DB bağlanamıyor
- `DATABASE_URL` yanlış/kapalı
- Coolify network/port erişimi

### 7.3 Log dosyası yok
- `COLLECTOR_LOG_FILE` path yanlış
- Volume mount yoksa container içinde dosya görünmez

### 8.4 Redeploy sonrası “tool yok / session hatası”
Belirti:
- `Tool '... not found'`
- `400 Missing session ID`
- `406 Not Acceptable`

Kök neden:
- `streamable-http` stateful session gerektirir ve redeploy sonrası eski session devam etmez.

Çözüm:
- Client tarafında yeniden `initialize` yap.
- `bash scripts/smoke_mcp.sh` ile uçtan uca doğrula.

---

## 9) Güvenlik notu

MCP ve Read API operasyonel veri içerir. Prod’da:
- Domain’i public bırakma
- IP allowlist veya basic auth uygula
- DB user’ı read-only yap (mümkünse)
