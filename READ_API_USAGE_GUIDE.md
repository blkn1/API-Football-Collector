## Read API Usage Guide (REST + SSE + Feature Store)

Bu doküman Read API’yi “tüketen herkes” için yazıldı: frontend, n8n, dashboard, NocoDB, feature engineering, ops.

Odak:
- **Hangi endpoint ne işe yarar?**
- **Hangi parametre neyi değiştirir?**
- **Hangi veriyi nereden alır (CORE/MART)?**
- **Ne zaman “sorun var” deriz, ne zaman false-positive?**
- **Basic Auth / IP allowlist nasıl doğru kullanılır?**

> Read API **read-only**’dir. API-Football quota tüketmez. Sadece Postgres’ten okur.

---

## Base URL ve OpenAPI

- **Base URL**: Coolify’de verdiğin domain (örn. `https://readapi.<domain>`)
- **OpenAPI UI**: `GET /docs`
- **OpenAPI JSON**: `GET /openapi.json`

> `/docs` en güncel “şema”yı verir ama **kullanım akışlarını** bu doküman anlatır.

---

## Strict query params (katı kurallar)

Read API artık **katı query param** modunda çalışır:

- Bir endpoint’in desteklemediği query param gönderirsen (örn. `/v1/fixtures?date=...&date_from=...`)
- Sunucu **400** döner ve param’ı “sessizce yok saymaz”.

Hata formatı (FastAPI HTTPException):

```json
{
  "detail": {
    "error": "unknown_query_params",
    "unknown": ["date_from"],
    "allowed": ["date", "league_id", "limit", "status"]
  }
}
```

Bu kural özellikle ActionsGPT/agent’ların “uydurma param” üretmesini engellemek için eklendi.

---

## Güvenlik (prod): IP allowlist + Basic Auth

Read API’de tüm uçlar `require_access` ile korunabilir:

### Basic Auth (opsiyonel ama önerilen)
ENV:
  - `READ_API_BASIC_USER`
  - `READ_API_BASIC_PASSWORD`

Davranış:
- Bu ikisi set edilirse **Basic Auth zorunlu** olur.
- Set değilse (dev kolaylığı için) Basic Auth **atlanır**.

Curl ile doğru kullanım:

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" "$READ_API_BASE/v1/health"
```

Postman ile doğru kullanım:
- Authorization → Type: **Basic Auth**
- Username/Password alanlarını doldur
- URL’ye `?user=...&pass=...` gibi ekleme yapma (yanlış + güvenlik riski)

### IP allowlist (opsiyonel)
ENV:
- `READ_API_IP_ALLOWLIST="1.2.3.4,5.6.7.8"`

Davranış:
- allowlist set edilirse, listede olmayan IP’ler **403 `ip_not_allowed`** alır.

Önemli not (proxy arkasında):
- Uygulama şu an IP’yi `request.client.host` üzerinden okur.
- Traefik/Cloudflare gibi proxy arkasında gerçek client IP yerine proxy IP görülebilir.
- Bu yüzden prod’da IP allowlist’i mümkünse **proxy/gateway katmanında** uygulamak daha güvenlidir.

---

## “Season” kuralı (çok önemli)

`/read/*` uçlarının çoğunda `season` zorunludur.

İki kullanım var:
- **Her request’te season ver**: en deterministik yöntem
- **Default season set et**: ENV ile

ENV:
- `READ_API_DEFAULT_SEASON=2025`

Davranış:
- `season` parametresi verilmezse `READ_API_DEFAULT_SEASON` kullanılır.
- Hiçbiri yoksa: **400 `season_required`**

---

## Veri katmanları: hangi endpoint nereden okuyor?

- **CORE**: normalize edilmiş tablolar (fixtures, teams, standings, injuries, top_scorers, team_statistics, fixture_details…)
- **MART**: hızlı okuma tabloları/view’lar (coverage_status, live_score_panel, daily_fixtures_dashboard…)

Read API:
- `/v1/*` uçları: dashboard/frontend için “ince” response’lar (daha stabil)
- `/read/*` uçları: modelleme / feature store için “curated” response’lar (league/country scoped, paging, filtre)
- `/ops/*`: ops panel ve sistem görünümü

---

## Önerilen hiyerarşi (UI / data exploration)

**Country → League → Season → Fixtures → Fixture Details**

1) Ülke listesi:
- `GET /read/countries?season=&q=&limit=&offset=`

2) Ülke içindeki lig listesi:
- `GET /read/leagues?country=<COUNTRY_OR_CODE>&season=&limit=&offset=`

3) Lig fixtures:
- `GET /read/fixtures?league_id=<LID>&season=<SEASON>&date_from=&date_to=&status=&team_id=&limit=&offset=`

4) Tek maç:
- `GET /read/fixtures/{fixture_id}`

5) Detaylar (goller, şut, korner, kadro, oyuncu istatistikleri):
- `GET /read/fixtures/{fixture_id}/events`
- `GET /read/fixtures/{fixture_id}/statistics`
- `GET /read/fixtures/{fixture_id}/lineups`
- `GET /read/fixtures/{fixture_id}/players`

---

## Hızlı smoke (script)

Repo içinde Read API smoke script’i var:
- `bash scripts/smoke_read_api.sh`

Örnek:

```bash
READ_API_BASE="https://readapi.<domain>" \
READ_API_BASIC_USER="user" READ_API_BASIC_PASSWORD="pass" \
bash scripts/smoke_read_api.sh
```

Curated uçları da denemek için:

```bash
READ_API_BASE="https://readapi.<domain>" \
READ_API_BASIC_USER="user" READ_API_BASIC_PASSWORD="pass" \
SMOKE_LEAGUE_ID=39 SMOKE_SEASON=2025 \
bash scripts/smoke_read_api.sh
```

---

## Endpoint kataloğu (detaylı)

Bu bölüm “tam liste + parametre + kullanım” amaçlıdır.

### 1) Health ve quota

#### `GET /v1/health`
Ne işe yarar:
- Servis ayakta mı?
- DB bağlantısı var mı?

Örnek response:
- `{ ok: true, db: true }`

#### `GET /v1/quota`
Ne işe yarar:
- Collector’ın son gördüğü rate-limit header’larını gösterir.

Not:
- `daily_remaining`/`minute_remaining` her zaman dolu olmak zorunda değil.

---

### 2) v1 fixtures (ince liste)

#### `GET /v1/fixtures`
Query params:
- `date=YYYY-MM-DD` (UTC date)
- `league_id=<int>` (opsiyonel)
- `status=<string>` (opsiyonel; `FT`, `NS`, `1H`…)
- `limit` (cap: 200)

Ne döner:
- Fixture listesi (id, league_id, date_utc, status, teams, goals, updated_at_utc)

Ne döNMEZ:
- events/lineups/statistics/players gibi detaylar (bunlar fixture details uçlarında)

Örnek:

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/v1/fixtures?date=2025-12-22&limit=200"
```

---

### 3) v1 team fixtures (takım sayfası için)

#### `GET /v1/teams/{team_id}/fixtures`
Query params:
- `from_date=YYYY-MM-DD` (UTC)
- `to_date=YYYY-MM-DD` (UTC)
- `status` (opsiyonel)
- `limit` (cap: 500)

Ne işe yarar:
- Takımın tüm turnuvalardaki (lig/kupa dahil) maçlarını tarih aralığında listeler.

---

### 4) v1 fixture details (tek endpoint, birleşik paket)

#### `GET /v1/fixtures/{fixture_id}/details`
Ne döner:
- `events`, `lineups`, `statistics`, `players` hepsi tek pakette.

Kaynak önceliği:
- Önce `core.fixture_details` snapshot (varsa)
- Yoksa normalize tablolardan fallback (`core.fixture_events`, `core.fixture_lineups`, `core.fixture_statistics`, `core.fixture_players`)

Ne zaman kullanılır:
- Frontend “match details” ekranında tek request ile her şeyi almak için.

---

### 5) v1 h2h (basit)

#### `GET /v1/h2h`
Query params:
- `home_team_id=<int>`
- `away_team_id=<int>`
- `limit` (cap: 50)

Ne döner:
- İki takım arasındaki maç listesi (goals, status, updated_at)

> Daha gelişmiş (summary’li) sürüm için `/read/h2h` kullan.

---

### 6) v1 team metrics (feature engineering için hazır özet)

#### `GET /v1/teams/{team_id}/metrics`
Query params:
- `last_n` (default 20, cap 50): sadece tamamlanmış maçlar
- `as_of_date=YYYY-MM-DD` (opsiyonel; bu tarihe kadar olan maçlar)

Ne döner:
- W/D/L, gol ortalamaları, BTTS/clean sheet oranları
- match statistics ortalamaları (shots, corners, cards, possession…)
- match statistics toplamları + kaç maçta mevcut olduğu (özellikle “son 20 maç toplam korner” için)
- `fixtures_sample`: örnek fixture listesi

Bu endpoint, “challenge” gibi kullan: model features için hızlı başlangıç.

Örnek: son 20 maç toplam korner

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/v1/teams/228/metrics?last_n=20"
```

Response içinde:
- `match_stats_sum.corner_kicks` = toplam korner (son 20 maç, sadece stats bulunan maçlar)
- `match_stats_count.corner_kicks` = korner stat’ı gelen maç sayısı

---

### 7) v1 standings / teams / injuries

#### `GET /v1/standings/{league_id}/{season}`
Ne döner:
- standings listesi (rank, points, goals_for/against, form…)

#### `GET /v1/teams`
Query params:
- `search` (opsiyonel; ILIKE)
- `league_id` (opsiyonel; o ligde fixtures’te görünen takımlar)
- `limit` (cap 200)

#### `GET /v1/injuries`
Query params:
- `league_id`, `season`, `team_id`, `player_id` (opsiyonel)
- `limit` (cap 200)

---

### 8) SSE (Server-Sent Events)

#### `GET /v1/sse/system-status`
Query params:
- `interval_seconds` (default 5, min 2, max 60)

Ne döner:
- `event: system_status` ile JSON payload
- payload: `{quota, db}` (MCP query’lerinden)

#### `GET /v1/sse/live-scores`
Query params:
- `interval_seconds` (default 3, min 2, max 30)
- `limit` (cap 500)

Kaynak:
- `mart.live_score_panel`

Not (deployment gerçeği):
- Bu projede live polling kapalı olabilir; o durumda SSE doğal olarak “sessiz/boş” görünür.

---

## Curated Feature Store: `/read/*` (league/country scoped)

Bu uçlar “keşif + paging + filtre” için tasarlanmıştır.

### 1) `GET /read/countries`
Query params:
- `season` (opsiyonel) → `core.leagues.seasons` JSONB içinde year match
- `q` (opsiyonel) → country_name / country_code search
- `limit` (cap 500), `offset`

Ne döner:
- `{ ok, items: [{country_name,country_code,country_flag,leagues_count}], paging }`

### 2) `GET /read/leagues`
Query params:
- `country` (opsiyonel; name veya code ile match)
- `season` (opsiyonel)
- `limit` (cap 500), `offset`

### 3) `GET /read/fixtures`
Query params:
- **Scope**: `league_id` **veya** `country` (en az biri zorunlu)
- `season` (zorunlu veya default)
- Filtreler:
  - `date_from=YYYY-MM-DD`
  - `date_to=YYYY-MM-DD`
  - `team_id=<int>`
  - `status=<string>` (status_short)
- Paging:
  - `limit` (cap 500), `offset`

Not:
- Bu endpoint “fixture index” döner. Events/statistics/lineups/players için alt uçlar kullanılır.

### 4) `GET /read/fixtures/{fixture_id}`
- Tek fixture index objesi (404: `fixture_not_found`)

### 5) Fixture details component uçları

#### `GET /read/fixtures/{fixture_id}/events`
- `limit` (cap 10000)
- Gol/kart/değişiklik gibi olaylar

#### `GET /read/fixtures/{fixture_id}/statistics`
- Team bazlı match statistics JSON

#### `GET /read/fixtures/{fixture_id}/lineups`
- Formation, start_xi, substitutes, coach…

#### `GET /read/fixtures/{fixture_id}/players`
Query params:
- `team_id` (opsiyonel; filtre)
- `limit` (cap 20000)

### 6) `GET /read/standings`
Query params:
- `league_id` (zorunlu)
- `season` (zorunlu veya default)

### 7) `GET /read/injuries`
Query params:
- Scope: `league_id` veya `country` (en az biri)
- `season` (zorunlu veya default)
- `team_id`, `player_id` (opsiyonel)
- Paging: `limit` (cap 1000), `offset`

### 8) `GET /read/top_scorers`
Query params:
- `league_id` (zorunlu)
- `season` (zorunlu veya default)
- `include_raw` (default true) → `raw` alanını kapatmak için false
- `limit` (cap 500), `offset`

### 9) `GET /read/team_statistics`
Query params:
- `league_id` (zorunlu)
- `season` (zorunlu veya default)
- `team_id` (opsiyonel)
- `include_raw` (default true)
- `limit` (cap 2000), `offset`

### 10) `GET /read/h2h` (summary’li)
Query params:
- `team1_id` (zorunlu)
- `team2_id` (zorunlu)
- `league_id` (opsiyonel)
- `season` (opsiyonel)
- `limit` (cap 200)

Ne döner:
- `items`: fixture listesi
- `summary_team1`: W/D/L ve goals_for/against (team1 perspektifi)

### 11) `GET /read/coverage`
Query params:
- `season` (zorunlu veya default)
- `league_id` (opsiyonel)
- `country` (opsiyonel)
- `endpoint` (opsiyonel; örn `"/fixtures"`)
- Paging: `limit` (cap 2000), `offset`

Özel: `flags`
- `flags.no_matches_scheduled=true` ise, lig “tatilde/off-season” olabilir ve `freshness_coverage` düşüklüğü tek başına alarm değildir.

---

## Ops endpoints

### `GET /ops`
- HTML dashboard

### `GET /ops/api/system_status`
JSON içinde şunları döner:
- `quota` (MCP: `get_rate_limit_status`)
- `db` (MCP: `get_database_stats`)
- `coverage_summary` (MCP: `get_coverage_summary`, season default env’den)
- `job_status` (MCP: `get_job_status`)
- `job_status_compact` (ops için compact view)
- `standings_progress` (MCP: `get_standings_refresh_progress`)
- `backfill` (MCP: `get_backfill_progress`)
- `raw_errors` (MCP: `get_raw_error_summary`)
- `raw_error_samples` (MCP: `get_raw_error_samples`, endpoint=/fixtures)
- `recent_log_errors` (MCP: `get_recent_log_errors`)

Ne zaman kullanılır:
- “tek bakışta sistem” ve hızlı debug

### `GET /ops/api/scope_policy`
Amaç: “Neden standings yok?” gibi sorularda **out-of-scope mı, yoksa pipeline sorunu mu** ayrımını hızlı yapmak.

Query params:
- `league_id` (zorunlu)
- `season` (opsiyonel; verilmezse MCP config’den infer eder)

Yanıt:
- MCP `get_scope_policy()` çıktısını aynen döner (`decisions[]` içinde endpoint bazında `in_scope/reason`).

---

## Hata kodları (Read API’nin “sözlüğü”)

Bu kodlar response body’de `detail` alanında görünür.

- **401**
  - `basic_auth_required`
  - `invalid_credentials`
- **403**
  - `ip_not_allowed`
- **400**
  - `season_required`
  - `invalid_season`
  - `league_id_or_country_required`
  - `invalid_date_format_expected_YYYY-MM-DD`
  - `to_date_must_be_gte_from_date`
- **404**
  - `fixture_not_found`

Bu kodlar “frontend/automation” tarafında doğru mesaj üretmek için kullanılmalı.

---

## Challenge: “historical team comparison” (goals, shots, corners) nasıl çıkarılır?

Hedef: iki takımı (veya bir takımı) geçmiş maçlarına göre karşılaştırmak.

### 0) Önce “data leakage” önlemi: as_of_date / cutoff mantığı
Eğer “tahmin” yapıyorsan, geleceği görmemek için her şeyi bir **cutoff** tarihine bağla:

- **Cutoff**: tahmin yapacağın maçın kickoff zamanından **önceki** bir an.
- Pratik: `as_of_date=YYYY-MM-DD` kullanıp o günün UTC “end-of-day”’ini cutoff kabul et.
- Amaç: “maç oynandıktan sonra güncellenen stats/events” gibi bilgileri feature’a yanlışlıkla karıştırmamak.

Bu yüzden aşağıdaki örneklerde sürekli şu pattern var:
- “Önce aday maçları listele”
- “Sonra o maçtan önceki dönem için feature çıkar”

### 1) Fixture listesini çek (tamamlanmış maçlar)
Örnek: belirli lig+sezon içinde, belirli takım ve tarih aralığı:

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/fixtures?league_id=39&season=2025&team_id=50&status=FT&date_from=2025-08-01&date_to=2025-12-31&limit=200&offset=0"
```

### 2) Her fixture için match statistics çek
- `GET /read/fixtures/{fixture_id}/statistics`

Buradan tipik olarak şunları toplarsın:
- `shots_on_goal`, `total_shots`, `corner_kicks`, `ball_possession`, `yellow_cards`…

### 3) Aggregation (uygulama tarafında)
Önerilen yaklaşım:
- aynı anda 200 fixture’a “sonsuz paralel” istek atma
- concurrency limit (örn. 5–10) koy
- sonuçları cache’le (fixture statistics değişmiyorsa tekrar çekme)

Alternatif (tek endpoint):
- Daha hızlı başlangıç için `/v1/teams/{team_id}/metrics?last_n=20` kullan (hazır ortalamaları verir).

---

## Challenge örnekleri (tahmin yapıyormuş gibi)

Bu bölümdeki örneklerin amacı şu: “bir maç için feature set nasıl çıkarılır?” sorusunu uçtan uca göstermek.
Hepsi tarih aralığı düşünülerek yazıldı.

### Örnek A — “Hafta sonu maçlarını tahmin edeceğim” (lig bazlı fixture aday listesi)
Amaç: Önce tahmin edeceğin maçları seçmek.

1) Hafta sonu (UTC) aday maç listesi:

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/fixtures?league_id=39&season=2025&date_from=2025-12-27&date_to=2025-12-28&limit=500&offset=0"
```

2) Bu listeden `status_short="NS"` olanları “predict set” yap.
> Bu noktada goals/score null olması normaldir; maç daha oynanmadı.

3) Her maç için `fixture_id`, `home_team_id`, `away_team_id`, `date_utc` değerlerini sakla.

### Örnek B — “Bir maç için iki takımın son-20 formunu çıkar” (leakage kontrollü)
Amaç: Bir maçın kickoff’undan önceki performansa bakarak feature üretmek.

Elimizde bir maç olsun:
- `fixture_id=1398685`
- `home_team_id=3584`
- `away_team_id=1002`
- kickoff tarihi: `2026-05-02` (örnek)

1) Home takım için hazır metrik (tamamlanmış maçlardan, cutoff ile):

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/v1/teams/3584/metrics?last_n=20&as_of_date=2026-05-01"
```

2) Away takım için aynı:

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/v1/teams/1002/metrics?last_n=20&as_of_date=2026-05-01"
```

Bu iki output’tan tipik feature’lar:
- W/D/L, win_rate
- gf_avg / ga_avg
- btts_rate, clean_sheet_rate
- shots_on_goal avg, corners avg, possession avg (varsa)

> Neden iyi: `as_of_date` sayesinde 2026-05-02 maçının sonucuna/istatistiğine “yanlışlıkla” bakmamış olursun.

### Örnek C — “Aynı lig içinde team-level season profile + sakatlık etkisi”
Amaç: Takımların sezon profili (team_statistics) + sakatlık yoğunluğu ile feature oluşturmak.

1) Lig içindeki iki takımın sezon profili (form gibi):

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/team_statistics?league_id=39&season=2025&team_id=3584&include_raw=false&limit=1"
```

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/team_statistics?league_id=39&season=2025&team_id=1002&include_raw=false&limit=1"
```

2) Aynı lig+sezonda sakatlık listesi (takım bazlı):

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/injuries?league_id=39&season=2025&team_id=3584&limit=200&offset=0"
```

Bu çıktıdan uygulama tarafında çıkarılabilecek basit feature:
- “injuries_count” (list length)
- severity distribution (eğer doluysa)

> Neden iyi: “form” (team_statistics) ile “availability” (injuries) aynı maç için birleşir.

### Örnek D — “Top scorers etkisi: gol tehdidi”
Amaç: Takımın gol tehdidini proxy’lemek.

1) Lig top scorers listesini çek:

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/top_scorers?league_id=39&season=2025&include_raw=false&limit=50&offset=0"
```

2) Bu listeden:
- home takımda oynayan oyuncu var mı?
- ilk 10’da kaç oyuncu var?
gibi “count-based” feature’lar üret.

> Neden iyi: Bu yaklaşım tek tek oyuncu fixture details’a girmeden “gol üretme kapasitesi”ne kaba bir sinyal sağlar.

### Örnek E — “Head-to-head (H2H) bias” (league/season filtreli)
Amaç: İki takım arasındaki geçmiş sonuçlardan “psikolojik/uyum” sinyali.

```bash
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/h2h?team1_id=3584&team2_id=1002&league_id=204&season=2025&limit=20"
```

Bu response’ta iki değer seti önemlidir:
- `summary_team1`: team1 perspektifinden W/D/L + gol ortalamaları
- `items`: tek tek maçlar

> Neden iyi: Aynı league+season filtresi, “10 yıl önceki alakasız maçları” karıştırmayı azaltır.

### Örnek F — “Training dataset üret: sezon içinde rolling windows”
Amaç: Model eğitimi için her maç için “maçtan önceki features” ve “label (sonuç)” üretmek.

Pratik yaklaşım (iteratif):
1) Sezondan FT maçları çek (label için):
- `/read/fixtures?league_id=<LID>&season=<S>&status=FT&date_from=...&date_to=...`
2) Her FT maç için cutoff’ı “maçtan 1 gün önce” kabul et:
- home: `/v1/teams/{home}/metrics?last_n=20&as_of_date=<fixture_date_minus_1>`
- away: `/v1/teams/{away}/metrics?...`
3) Label:
- maçtaki `goals_home/goals_away` (fixtures item’dan)

> Neden iyi: Her satırda cutoff aynı mantıkla uygulanır; leakage riski düşük, dataset tutarlı.

---

## Notlar ve “niçin böyle”

### Neden `/read/fixtures` gol/score bazen boş?
- `status=NS` ise goals ve score doğal olarak null döner.
- Detaylar (events/statistics/lineups/players) maç oynandıktan sonra ve details job’ları koştuktan sonra dolmaya başlar.

### Neden bazı FT maçlar “pending/not_found” görünebilir?
`/read/fixtures` ve `/read/fixtures/{fixture_id}` artık verification alanlarını da döner:
- `needs_score_verification`
- `verification_state` (`pending|verified|not_found|blocked`)
- `verification_attempt_count`
- `verification_last_attempt_at_utc`

Anlamı:
- **pending**: maç FT görünüyor ama upstream API’dan tekrar doğrulama bekliyor (job retry/cooldown ile dener)
- **verified**: upstream’dan doğrulanmış
- **not_found**: upstream API 200 dönse bile `response=[]` veriyor (kaynak veri yok → bizim tarafta “doğru skor/events” üretilemez)

## Veri kalite / operasyon filtreleri (Read API)

Read API artık `/read/fixtures` üzerinde “kalite/operasyon” amaçlı filtreleri destekler.
Bu filtreler **Read API’nin kendisi için güvenlik değildir** (Read API zaten auth ile korunur); amaç:
- “Eksik var mı?” hızlı tarama
- “Kaynak API boş mu dönüyor?” ayırımı
- “Details (events/lineups/statistics/players) dolu mu?” kontrol

### 1) Verification filtreleri

`GET /read/fixtures?...` için yeni query params:
- `needs_score_verification=true|false`
- `verification_state=pending|verified|not_found|blocked`
- `min_verification_attempt_count=<int>`

Örnekler:

```bash
# 1) Kaynak API boş döndüğü için 'not_found' olmuş fixture'ları bul
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/fixtures?league_id=274&season=2025&status=FT&verification_state=not_found&limit=200&offset=0"

# 2) Hâlâ pending olanları bul (retry bekleyen)
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/fixtures?league_id=274&season=2025&status=FT&verification_state=pending&limit=200&offset=0"

# 3) En az 2 kez denenmiş pending'leri bul
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/fixtures?league_id=274&season=2025&status=FT&verification_state=pending&min_verification_attempt_count=2&limit=200&offset=0"
```

### 2) Details var/yok filtreleri

`GET /read/fixtures?...` için yeni query params:
- `has_events=true|false`
- `has_lineups=true|false`
- `has_statistics=true|false`
- `has_players=true|false`

Örnekler:

```bash
# 1) FT maçlarda events eksik olanları bul
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/fixtures?league_id=39&season=2025&status=FT&has_events=false&limit=200&offset=0"

# 2) FT + verified ama events yok (pipeline gap avı)
curl -u "$READ_API_BASIC_USER:$READ_API_BASIC_PASSWORD" \
  "$READ_API_BASE/read/fixtures?league_id=39&season=2025&status=FT&verification_state=verified&has_events=false&limit=200&offset=0"
```

### Neden coverage bazen “stale” ama sorun yok?
- Lig tatilde/off-season olabilir.
- Bu durumda `/read/coverage` içindeki `flags.no_matches_scheduled` sana “false-positive olabilir” sinyalini verir.

---

## Doküman kaynakları (repo içi)

- Frontend odaklı sözleşme: `READ_API_FRONTEND_CONTRACT.md`
- Operasyon/runbook: `PRODUCTION_RUNBOOK.md`
- MCP gözlem rehberi: `MCP_USAGE_GUIDE.md`
- Smoke script: `scripts/smoke_read_api.sh`


