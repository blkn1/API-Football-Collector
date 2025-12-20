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
- `get_job_status(job_name=None)`
  - Job config + collector log tail merge.
- `get_coverage_summary(season=None)`
- `get_coverage_status(league_id=None, season=None)`

### 5.2 Backfill + hata gözlemi
- `get_backfill_progress(job_id=None, season=None, include_completed=False, limit=200)`
- `get_raw_error_summary(since_minutes=60, endpoint=None, top_endpoints_limit=25)`
- `get_recent_log_errors(job_name=None, limit=50)`

### 5.4 Live loop gözlemi
- `get_live_loop_status(since_minutes=5)`
  - Prod’da `ENABLE_LIVE_LOOP=1` olduğunda `/fixtures?live=all` polling’in RAW’a düştüğünü doğrular.
  - Redeploy sonrası “live loop açık mı?” sorusunun en net cevabı.

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
5) `get_live_loop_status(since_minutes=5)`
   - Beklenen:
     - `ENABLE_LIVE_LOOP=1` ise `running=true` ve `requests>0`
     - `ENABLE_LIVE_LOOP=0` ise `running=false`
6) `get_daily_fixtures_by_date_status(since_minutes=180)`
   - Beklenen:
     - `daily_fixtures_by_date` cron’u çalışıyorsa `running=true` ve `last_fetched_at_utc` son 30–60 dk içinde güncellenir (*/30 ayarında).
   - Not: Bu tool log parse’a dayanmaz; doğrudan RAW’dan kanıtlar.
   - Alanlar (global_by_date için kritik):
     - `global_requests`: date-only (league filtresiz) istek sayısı
     - `pages_fetched`: global date-only isteklerde sayfa sayısı (page paramı yoksa 1 sayılır)
     - `max_page`: görülen en yüksek sayfa
     - `results_sum`: global date-only isteklerde toplam results (fixture sayısı)
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
- Live loop açık ise `get_live_loop_status().running=true`.
- Daily fixtures cron ayarlı ise `get_daily_fixtures_by_date_status().running=true`.

FAIL kriteri:
- `get_database_stats()` DB error / exception
- `get_raw_error_summary()` 429/5xx/envelope_errors yükseliyor
- Live loop açık olmasına rağmen `get_live_loop_status().running=false` (deploy/env sorunu)
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
