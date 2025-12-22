## MCP Usage Guide (API-Football Collector)

Bu dokümanın amacı “tool isimlerini listelemek” değil; **sistemi gerçekten görüp doğrulamanı sağlamak**.
MCP’yi doğru kullandığında şu sorulara **kanıtla** cevap verirsin:

- “Job çalıştı mı, yoksa ben yanlış pencereden mi bakıyorum?”
- “Bu coverage alarmı gerçek mi, yoksa ligde maç yok diye mi stale görünüyor?”
- “RAW var mı, CORE’a yazılmış mı, MART güncellenmiş mi?”
- “Quota bitiyor mu, 429/5xx var mı, sistem güvenilir mi?”

> MCP bu projede **read-only gözlem katmanı**dır. Job tetiklemez ve DB’ye yazmaz. Yazma işi collector/job katmanındadır.

---

## MCP ile teşhis modeli: tek metrikle karar verme (yanlış)

Bu projede bir sorunu teşhis etmek için her zaman 3 kanıt kaynağını birlikte okuruz:

- **Config kanıtı**: “Bu lig/endpoint izleniyor mu?”  
  Kaynak: `config/jobs/*.yaml` (tracked leagues, season, job interval, enable/disable)

- **RAW kanıtı**: “Gerçekten API çağrısı yapılmış mı?”  
  Kaynak: `raw.api_responses` (MCP tool’ları çoğunlukla RAW’dan kanıt üretir)

- **CORE kanıtı**: “Veri normalize edilip UPSERT ile yazılmış mı?”  
  Kaynak: `core.*` tabloları (fixtures, standings, injuries, top_scorers, team_statistics, fixture_details…)

En sık yapılan hata:
- Sadece coverage’a bakıp “job çalışmıyor” demek
- Sadece log’a bakıp “çalışıyor” demek

Doğru yaklaşım: **RAW + CORE + config** üçlüsü ile karar ver.

---

## `/ops` vs MCP: hangisi ne zaman?

- **`/ops` (Read API)**: tek bakışta sistem (dashboard)
  - Ne zaman: redeploy sonrası hızlı smoke, günlük kontrol
  - Ne verir: quota, db stats, coverage summary, job status (kompakt), raw error summary…

- **MCP**: kanıta dayalı derin teşhis
  - Ne zaman: coverage alarmı, lig bazında veri kontrolü, job “gerçekten request atıyor mu?”, backfill/standings progress…

> `/ops/api/system_status` zaten MCP’den veri çeker; ama MCP’de çok daha fazla tool ve daha detaylı teşhis akışı var.

---

## Bağlantı modları: Claude Desktop (stdio) ve Prod (streamable-http)

### Claude Desktop (local) → stdio
Claude Desktop MCP’yi genelde **stdio** ile çalıştırır. Debug için en kolay yol budur.

Örnek config (`~/.config/Claude/claude_desktop_config.json`):

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

**ENV notları**
- **`DATABASE_URL`**: MCP’nin okuyacağı Postgres.
- **`COLLECTOR_LOG_FILE`**: `get_job_status()` ve `get_recent_log_errors()` için JSONL log path.
- **Opsiyonel**:
  - `API_FOOTBALL_DAILY_CONFIG`: MCP’nin season/tracked leagues okuduğu config (default: `config/jobs/daily.yaml`)

### Prod → streamable-http (Traefik)
Prod MCP HTTP üzerinden **streamable-http** çalışır ve **stateful session** ister:
- önce `initialize`
- sonra aynı `mcp-session-id` ile `tools/list` / `tools/call`

Claude Desktop doğrudan HTTP transport konuşmadığı için prod’a bağlanırken **stdio→streamable-http adapter** kullanılır.

Örnek:

```json
{
  "mcpServers": {
    "api-football": {
      "command": "npx",
      "args": ["-y", "@pyroprompts/mcp-stdio-to-streamable-http-adapter"],
      "env": { "URI": "https://mcp.zinalyze.pro/mcp" },
      "timeout": 30000,
      "initTimeout": 20000
    }
  }
}
```

> Redeploy sonrası MCP restart olur → eski `mcp-session-id` geçersiz olabilir. Bu durumda tekrar `initialize` gerekir.

---

## “MCP’de hangi tool’lar var?” (dokümana değil MCP’ye bak)

Bu bölüm iki sorunu aynı anda çözer:

- **Tool keşfi**: “Prod’da hangi MCP tool’ları var?” (doküman drift’ini bitirir)
- **Session problemi**: “Claude Desktop bazen neden çalışmıyor? Neden ‘yeni session’ gerekiyor?”

### 1) Neden Claude Desktop bazen “çalışmıyor”?
Prod MCP `streamable-http` olduğu için **stateful** çalışır:

- Client önce **initialize** yapar
- Server response’ta bir **`mcp-session-id`** döner
- Client bundan sonra tüm `tools/list` ve `tools/call` isteklerinde bu session id’yi header’da taşır

**Redeploy** olduğunda MCP container restart olur ve:
- eski session’lar **geçersiz** olabilir
- bazı client’lar (özellikle adapter/desktop) eski session’ı “kafasında” tutup tekrar kullanmaya çalışır
- sonuç: Claude’da “tool yok”, “session hatası”, “hiçbir şey dönmüyor” gibi problemler

Kısa çözüm:
- Claude Desktop’ı tamamen kapat/aç (veya MCP bağlantısını kapatıp yeniden bağlan) → client yeniden initialize yapar.

### 2) Tool listesini (ve session’ı) en garanti şekilde nasıl doğrularım?
Prod’a karşı en sağlam doğrulama: `scripts/smoke_mcp.sh`.

Bu script şunları otomatik yapar:
- **initialize** → her çalıştırdığında **yeni session** alır
- **tools/list** → o an prod’da hangi tool’lar varsa listeler
- birkaç **tools/call** → MCP’nin gerçekten cevap ürettiğini kanıtlar

Çalıştır:
- `bash scripts/smoke_mcp.sh`

Eğer domain/path farklıysa override:
- `MCP_BASE_URL="https://mcp.example.com" MCP_PATH="/mcp" bash scripts/smoke_mcp.sh`

Bu script çalışıyorsa ama Claude çalışmıyorsa, sorun büyük ihtimalle:
- Claude/adapter’ın **eski session’ı tutması**
- veya yanlış endpoint’e bağlanması

Bu durumda pratik adım:
- Claude Desktop restart
- adapter URI kontrolü (doğru `/mcp` path)
- tekrar `tools/list`

### 3) “bash çalışmıyor / script koşamıyorum” durumunda ne yapacağım?
Bu script’i **MCP’ye erişebilen herhangi bir makinede** çalıştırabilirsin (local laptop dahil). Gerekenler:
- `bash`, `curl`, `awk`, `sed`, `tr`

Eğer script’i hiç koşturamıyorsan (ör. container’da bash yok), en basit yöntem:
- script’i local’de koş (genelde en hızlısı)

> Amaç teknik detay değil: “initialize → session id al → tools/list çağır” akışını mutlaka doğrulayacak bir yol bulmak.

### 4) Hızlı kontrol listesi (en sık hatalar)
- **Redeploy sonrası** Claude bozulduysa: önce Claude restart → sonra `tools/list`.
- `scripts/smoke_mcp.sh` “initialize did not return mcp-session-id” diyorsa:
  - yanlış `MCP_PATH` (genelde `/mcp`)
  - reverse proxy yanlış yönlendiriyor
  - veya istek `Accept: application/json, text/event-stream` olmadan gidiyor (script bunu zaten doğru set ediyor)

---

## Kritik kavram: `since_minutes` penceresi job interval ile uyumlu olmalı

“Job çalışmıyor” alarmlarının büyük kısmı aslında **yanlış pencere** yüzünden false-positive olur.

Örnek:
- `daily_fixtures_by_date` cron’u günde 1 kez çalışıyorsa,
  `get_daily_fixtures_by_date_status(since_minutes=180)` çağrısı “son 3 saatte request yok” döner.
  Bu, job çalışmıyor demek değildir; **normaldir**.

Doğru kullanım:
- Günlük işler için pratik: `since_minutes=1440` (son 24 saat)
- Saatlik işler için: 60–180 aralığı genelde anlamlı

Kural:
- “Job çalıştı mı?” sorusundan önce “Ben doğru pencereye bakıyor muyum?” sor.

---

## Playbook 1: Redeploy sonrası “Sistem ayakta mı?” (kanıtlı smoke)

Amaç: “DB erişilebilir mi, quota okunuyor mu, RAW hata profili temiz mi?”

- **Adım 1 — DB bağlantısı ve tablo görünürlüğü**
  - Tool: `get_database_stats()`
  - Neden: DB kopuksa downstream her şey bozulur; en temel kanıt budur.

- **Adım 2 — Quota**
  - Tool: `get_rate_limit_status()`
  - Neden: rate limiter state okunuyor mu, quota bitmiş mi?
  - Not: `minute_remaining` bazen header gelmediği için `None` olabilir; tek başına fail değildir.

- **Adım 3 — RAW hata profili**
  - Tool: `get_raw_error_summary(since_minutes=60)`
  - Neden: 429/5xx/envelope_errors artıyorsa sistem “çalışıyor gibi” görünse bile veri güvenilmez.

- **Adım 4 — Job kanıtı (best-effort)**
  - Tool: `get_job_status()`
  - Neden: config + log + RAW fallback birleşik “son görülme” kanıtı verir.
  - İpucu: log yoksa bile `last_seen_source="raw"` güçlü kanıttır.

PASS:
- Tool’lar exception üretmiyor (`ok=true`)
- RAW errors anormal yükselmiyor

---

## Playbook 2: Coverage alarmı → gerçek mi false-positive mi?

Coverage satırlarını doğru okumak için şu ayrımı yap:

- **`freshness_coverage`**: “son güncelleme ne kadar eski?”
- **`pipeline_coverage`**: “RAW akıyor mu + CORE doluyor mu?”
- **`count_coverage`**: “beklenen fixture sayısına göre coverage”  
  Bu sadece `config/coverage.yaml -> expected_fixtures` içinde tanımlı liglerde anlamlıdır.

### 2.A Önce doğru sezonu doğrula
`get_coverage_status(season=None)` çağrısında MCP, season’ı `config/jobs/daily.yaml` içinden alır.
Yanlış sezona bakarsan doğru lig bile “boş/stale” görünebilir.

### 2.B Lig tracked mı?
- Tool: `list_tracked_leagues()`
- Neden: tracked olmayan ligde coverage düşükse bu “sorun” değil, kapsam dışıdır.

### 2.C “Stale fixtures” alarmında en kritik soru: ligde maç var mıydı?
Senin Championship örneğinde olan durum:
- 20 Aralık’tan beri maç yok → `core.fixtures.updated_at` değişmiyor → `lag_minutes` büyüyor → freshness düşüyor
- Bu pipeline arızası değildir

Bu false-positive’i azaltmak için `mart.coverage_status.flags` eklendi.
`get_coverage_status()` çıktısında `/fixtures` satırında:
- **`flags.no_matches_scheduled=true`** görürsen: “Bu pencerede maç yok, freshness düşüklüğünü alarm gibi okuma.”

Önemli güvenlik kuralı:
- Bu flag sadece **`actual_count > 0`** olduğunda devreye girer.
- Yani “hiç fixture yok” gibi gerçek arızaları maskelemez.

### 2.D Alarmı kanıtla: RAW + CORE
Bu aşamada “gerçek sorun mu?” sorusuna karar verirsin.

- **CORE kanıtı (ligde FT maç var mı?)**
  - Tool: `query_fixtures(league_id=<LID>, status="FT", limit=20)`
  - Neden: FT maç yoksa ve olmalıysa ingestion eksik olabilir (ama lig tatildeyse zaten FT yoktur).

- **RAW kanıtı (job gerçekten request atmış mı?)**
  - Tool: `get_daily_fixtures_by_date_status(since_minutes=1440)` (günlük job için)
  - Neden: log’a değil, RAW’a bakarak “gerçek request var mı?” kanıtlanır.

- **Endpoint genel sync**
  - Tool: `get_last_sync_time(endpoint="/fixtures")`
  - Neden: tüm sistemde /fixtures endpoint’i ne kadar taze?

Karar:
- RAW yok + CORE stale → scheduler/job sorunu
- RAW var + CORE stale → transform/upsert sorunu
- RAW var + CORE var ama ligde maç yok → **false-positive** (flags ile doğrulanır)

---

## Playbook 3: `daily_fixtures_by_date` gerçekten çalışıyor mu? (doğru pencereyle)

Tool: `get_daily_fixtures_by_date_status(since_minutes=...)`

Ne ölçer?
- Log parse’a güvenmez.
- RAW’da `/fixtures?date=YYYY-MM-DD` çağrılarını kanıtlar.

Nasıl sorulmalı?
- Job günlükse: `since_minutes=1440`
- Job daha sık ise: interval’a göre (örn. 180)

`fixtures_fetch_mode=per_tracked_leagues` iken:
- `global_requests/pages_fetched/max_page/results_sum` 0/None olabilir → bu normaldir.

---

## Playbook 4: “Yeni dataset doluyor mu?” (top_scorers / team_statistics / fixture details)

Amaç: “job çalıştı mı?” değil; **RAW→CORE pipeline tamamlandı mı?**

- **Adım 1 — CORE sayımı**
  - Tool: `get_database_stats()` (core_top_scorers, core_team_statistics vb.)
  - Neden: CORE dolmuyor ise FE/Read API de bir şey gösteremez.

- **Adım 2 — Coverage drilldown**
  - Tool: `get_coverage_status(league_id=<LID>, season=<SEASON>)`
  - Neden: freshness + pipeline side-by-side görünür.

- **Adım 3 — Cron’u beklemeden doğrulama (collector)**
  - Not: MCP read-only olduğu için job tetiklemez.
  - Collector terminal:
    - `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=top_scorers_daily python3 scripts/run_job_once.py`
    - `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=team_statistics_refresh python3 scripts/run_job_once.py`

---

## Backfill ve standings batching: “tam tur bitti mi?”

- **Backfill**
  - Tool: `get_backfill_progress(...)`
  - Neden: “pending task kaldı mı?” sorusunun tek güvenilir kanıtı.

- **Standings batching/cursor**
  - Tool: `get_standings_refresh_progress(job_id="daily_standings")`
  - Neden: job parça parça çalışıyorsa “nerede kaldı, tam tur bitti mi?”
  - Okunacak alanlar:
    - `cursor`, `total_pairs`
    - `lap_count`
    - `last_full_pass_at_utc`

---

## Sık görülen sorunlar (ve gerçek nedeni)

### `season_required`
Neden: bazı tool’lar season olmadan anlamlı query üretemez.
Çözüm:
- tool’a `season=...` ver
- veya `config/jobs/daily.yaml` içine `season:` koy

### “Job çalışmıyor” sandım ama çalışıyormuş
Neden: `since_minutes` job interval’ı ile uyumlu değil.
Çözüm:
- günlük işler için `since_minutes=1440`

### Coverage “stale” ama lig tatilde (false-positive)
Neden: ligde maç yok; `updated_at` doğal olarak değişmiyor.
Çözüm:
- `/fixtures` coverage satırında `flags.no_matches_scheduled` kontrol et

---

## Şema / deploy notu (flags alanı)

Coverage satırlarına açıklama eklemek için `mart.coverage_status.flags (JSONB)` eklendi.
Bu değişiklik üretimde aktif olması için DB şemasının uygulanmış olması gerekir.

- Coolify/collector tarafında: `python3 scripts/apply_schemas.py` (repo yaklaşımınıza göre)

---

## Güvenlik notu

MCP ve Read API operasyonel veri içerir. Prod’da:
- MCP endpoint’ini public bırakmayın (IP allowlist / auth)
- DB user’ını mümkünse read-only yapın


