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

Claude’a şu sırayla tool çağırmasını söyle:
1) `get_database_stats()` → DB bağlantısı ve sayımlar geliyor mu?
2) `get_rate_limit_status()` → daily/minute remaining doluyor mu?
3) `get_job_status()` → job listesi + log bilgisi geliyor mu?
4) `get_backfill_progress()` → backfill state görülebiliyor mu?
5) `get_raw_error_summary(since_minutes=60)` → son 60 dk health summary

PASS kriteri:
- İlk 3 tool `ok=true` dönüyor ve exception yok.

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
