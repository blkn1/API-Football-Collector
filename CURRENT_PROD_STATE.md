# Current Production State (v3) — 83 League Rollout Ready

Bu doküman, şu ana kadar yaptığımız değişikliklerin **son halini** ve wave rollout öncesi operasyonel checklist’i özetler.

## 1) Stack ve servisler (docker-compose.yml)

- **postgres**: RAW/CORE/MART source of truth
- **collector**: APScheduler + jobs (`config/jobs/*.yaml`) + quota-safe istekler + RAW→CORE→MART
- **mcp**: read-only monitoring (Claude Desktop dahil)
- **read_api**: dış uygulamalar için read-only REST + SSE
- **live_loop**: opsiyonel; `ENABLE_LIVE_LOOP=1` olmadıkça API çağırmaz

Prod network notu:
- Prod’da **Postgres/Redis host port publish edilmez** (güvenlik + log gürültüsü önleme).
- MCP prod’da Traefik/Coolify üzerinden domain+path ile yayınlanır: `https://<SERVICE_FQDN_MCP>/mcp`.

## 2) Config yaklaşımı (hard-code yok)

- **Tracked leagues**: `config/jobs/daily.yaml -> tracked_leagues[]`  
  - Her item: `{id, name, season}` (**per-league season**)
- **Overrides**: `config/league_overrides.yaml` (ambiguous league mapping için deterministik)
- **Resolver çıktısı**: `config/resolved_tracked_leagues.yaml` (audit amaçlı)
- **Rate limiter**: `config/rate_limiter.yaml` (soft cap + emergency stop)

## 2.1 Günlük fixtures modeli: per_tracked_leagues (fixtures_fetch_mode)

Prod’da “bültende maç var ama sistemde yok” sorunu için `daily_fixtures_by_date` artık **global-by-date** modunda çalışır:
- Config: `config/jobs/daily.yaml` en üst satır:
  - `fixtures_fetch_mode: per_tracked_leagues`
- Davranış: `GET /fixtures?date=YYYY-MM-DD` (gerekirse paging)
- Sonuç: tracked olmayan kupalar/UEFA gibi competition’lar da “o gün” oynanıyorsa CORE’a düşer.

Deterministik kanıt (örnek):
- RAW (league filter olmayan global çağrı):
  - `requested_params={"date":"2025-12-18","timezone":"UTC"}`
  - `results=97`, `response_len=97`
- CORE:
  - `DATE(core.fixtures.date UTC)='2025-12-18'` için `COUNT(*)=96`

## 3) Backfill stratejisi (SeçenekB)

- Varsayılan backfill pairs:
  - `(league, current)` + `(league, current-1)`
  - `current = tracked_leagues[].season`
- Fixtures backfill:
  - Windowed: `/fixtures?league=X&season=Y&from=...&to=...`
  - Resume: `core.backfill_progress.next_page` (window index)
- Standings backfill:
  - `/standings?league=X&season=Y`
  - Resume: `core.backfill_progress.completed`

## 4) Dakikalık rateLimit kalıcı çözümü

- **Token bucket**: startup burst engellendi (bucket default **0 token** ile başlar; burst yok)
- **/teams cache**: `core.team_bootstrap_progress`
  - Aynı `(league_id, season)` için `/teams` bir kere başarılı ise tekrar çağrılmaz

## 4.1 MCP prod transport (Traefik + streamable-http)

- Prod env:
  - `MCP_TRANSPORT=streamable-http`
  - `MCP_MOUNT_PATH=/mcp`
- Streamable HTTP MCP **stateful**:
  - `Accept: application/json, text/event-stream`
  - `mcp-session-id` header’ı ile devam
  - Önce `initialize`, sonra `tools/list`
  - **Redeploy sonrası** eski session’lar geçersiz olur (yeniden initialize gerekir)

Hızlı doğrulama: `MCP_USAGE_GUIDE.md` bölüm 5.
Tam smoke: `bash scripts/smoke_mcp.sh`

Claude Desktop notu:
- Prod MCP `streamable-http` olduğu için Claude Desktop’a bağlamak için **stdio→streamable-http adapter** gerekir.
- Config: `MCP_USAGE_GUIDE.md` → bölüm 4.

## 5) Önemli tablolar

- RAW: `raw.api_responses`
- CORE:
  - `core.leagues`, `core.teams`, `core.venues`, `core.fixtures`, `core.standings`, `core.injuries`
  - `core.fixture_details` + `core.fixture_*` (events/players/statistics/lineups)
  - `core.backfill_progress` (resume state)
  - `core.team_bootstrap_progress` (dependency cache)
- MART:
  - `mart.coverage_status`
  - `mart.live_score_panel` (VIEW)

## 6) Wave rollout (wave1/wave2)

Wave helper:

```bash
python3 scripts/apply_league_wave.py --size 10
python3 scripts/apply_league_wave.py --size 25 --offset 10
```

Wave sonrası:
- Coolify redeploy (collector)
- 30–60 dk gözlem

## 7) 30–60 dk gözlem checklist (MCP)

- `get_backfill_progress()` → ilerleme var mı?
- `get_raw_error_summary(since_minutes=60)` → 429/5xx artıyor mu?
- `get_rate_limit_status()` → minute/daily trend
- `get_database_stats()` → core.fixtures/core.teams artıyor mu?

## 7.1 Live panel doğrulaması (UECL örneği)

UEFA Europa Conference League (UECL) prod live panel için league_id:
- `core.leagues` → `UEFA Europa Conference League` = **848**

DB kanıtı (live sırasında):
- `SELECT COUNT(*) FROM mart.live_score_panel WHERE league_id=848;`

## 8) Kullanılmayan / redundant dosyalar (silme önerisi)

Bu repo artık root Compose + root Dockerfile kullanıyor:
- ✅ Kullanılan: `docker-compose.yml`, `Dockerfile`

Silinebilir (redundant/legacy):
- `docker-compose.live.yml` (root compose zaten `live_loop` içeriyor)
- `docker/` klasörü komple:
  - `docker/docker-compose.yml`
  - `docker/Dockerfile`

Silmeden önce: Coolify build/deploy ayarlarında root `docker-compose.yml` ve root `Dockerfile` kullanıldığına emin ol.

