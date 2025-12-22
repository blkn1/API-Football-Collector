## Config Usage Guide (YAML + ENV) — API-Football Collector

Bu doküman, bu projedeki **tüm config yaklaşımının ana fikrini**, kurallarını ve prod’da nasıl uygulanacağını anlatır.

Hedef: davranışı **kod değişmeden** config ile yönetmek (config-driven), böylece prod daha güvenli ve sürdürülebilir olur.

---

## 1) Ana fikir: “Config-driven” ne demek?

Bu projede **league id / season / cron / quota limit / endpoint scope** gibi her kritik karar:
- Kod içine yazılmaz (**hard-code yok**)
- YAML ve ENV üzerinden belirlenir (**config-driven**)

Neden?
- Prod’da yeni lig eklemek / cadence değiştirmek = **sadece config change + redeploy**
- Yanlış/eksik değişiklik riski azalır
- Quota ve rate-limit kontrolü config üzerinden yönetilir

---

## 2) Config kaynakları ve öncelik sırası

Bu projede üç ana config kaynağı vardır:

1) **ENV (Coolify)**  
   - Runtime behavior: enable/disable servisler, timezone, path override, DB URL vb.

2) **YAML dosyaları (`config/*.yaml`)**  
   - Job schedule, tracked leagues, quota/rate-limiter, API base settings

3) **Kod (default fallback)**  
   - Sadece “config yoksa safe fallback” için.
   - Örn: scheduler job dosyalarını `config/jobs/*.yaml` altında arar.

Kural: **ENV sadece override içindir; kalıcı davranış YAML’da olmalı.**

---

## 3) Konfigürasyon dosyaları: ne işe yarar?

### 3.1 `config/api.yaml`
- API base URL, API key env adı, timeout gibi HTTP client ayarları.

### 3.2 `config/rate_limiter.yaml`
- Dakika bazlı token bucket (`minute_soft_limit`)
- Günlük emergency stop threshold (`emergency_stop_threshold`)

Kural:
- API çağrısı öncesi token alınır
- Her response’ta header’lardan quota state güncellenir

### 3.3 `config/coverage.yaml`
- Coverage hesaplama/targets (endpoint/league/season kapsamı) ve freshness/lag kuralları.

### 3.4 `config/jobs/daily.yaml`
Bu dosya prod’daki “operasyonel gerçeklik”tir:
- **tracked_leagues**: hangi ligleri takip ediyoruz (id + season + name)
- **incremental_daily job’lar**: fixtures/standings/injuries/fixture_details/backfill cadence

Kural:
- “Yeni lig takip edilecekse” tek doğru yer: **`daily.yaml tracked_leagues`**

#### 3.4.1 fixtures_fetch_mode (bülten gap çözümü)
`config/jobs/daily.yaml` en üstünde:
- `fixtures_fetch_mode: per_tracked_leagues` (bu repo default)

Bu ayar şunu değiştirir:
- `per_tracked_leagues`: `/fixtures?league&season&date` → sadece tracked ligler
- `global_by_date`: `/fixtures?date=YYYY-MM-DD` (gerekirse paging) → o gün oynanan **kupalar/UEFA dahil** tüm fixtures

Prod doğrulama (MCP):
- `get_daily_fixtures_by_date_status()` içinde `requests>0` (per-league modda `global_requests` 0 olabilir)

#### 3.4.2 stale_live_refresh (stale canlı statü temizleme)
Amaç: CORE’da yanlışlıkla “canlı” gibi kalan (örn. `1H/2H/HT/INT/SUSP`) ama uzun süredir güncellenmeyen fixture’ları periyodik olarak tekrar çekip temizlemek.

Bu job:
- DB’den **stale görünen** fixture id’lerini seçer (`threshold_minutes` ile)
- `/fixtures?ids=...` ile (max 20) tekrar fetch eder
- RAW’a yazar + CORE fixtures UPSERT eder

Scope (hangi liglerde çalışır?) **config-driven**:
- `params.scope_source: daily` → `config/jobs/daily.yaml -> tracked_leagues`
  - Not: Bu deployment’ta `live.yaml`/live loop yok.

Prod kuralı:
- `scope_source: live` seçersen `live.yaml` içindeki `filters.tracked_leagues` **boş olamaz**.
  - Live loop için boş liste “track all” anlamına gelebilir; ama stale refresh için bu **quota-risk** olduğu için intentionally reddedilir.

#### 3.4.3 daily_standings “parça parça” (batch) çalıştırma
Çok fazla tracked lig varsa `/standings` job’ı tek seferde uzun sürebilir ve quota’yı tek anda tüketebilir.
Bu repo bunun için **cursor-based batching** destekler:

- `config/jobs/daily.yaml` içinde:
  - `jobs[daily_standings].mode.max_leagues_per_run: <N>`
- Davranış:
  - Her çalıştırmada sadece N adet (league,season) işlenir.
  - Cursor CORE’da tutulur: `core.standings_refresh_progress` (wrap-around).

Gözlem:
- MCP:
  - `get_standings_refresh_progress(job_id="daily_standings")`
  - `get_last_sync_time(endpoint="/standings")`
- Ops:
  - `/ops/api/system_status` → `standings_progress`

“Bitti mi?” (net kanıt):
- `get_standings_refresh_progress()` içinde:
  - `lap_count >= 1` → en az 1 tam tur tamamlandı
  - `last_full_pass_at_utc` → son tam turun zamanı

### 3.5 Live loop (legacy)
Bu deployment’ta live polling servisleri (`live_loop`, `redis`) compose’tan kaldırıldı.
- Repo’da `scripts/live_loop.py` dosyası **legacy** olarak durabilir ama prod’da çalıştırılmıyor.

### 3.6 `config/jobs/static.yaml`
- Countries/timezones gibi “static bootstrap” job’ları.
- Leagues/teams bootstrap job’ları prod’da genelde **disabled** tutulur (quota + churn).

Önemli iyileştirme (config-driven inheritance):
- `bootstrap_leagues` ve `bootstrap_teams` için `tracked_leagues: []` bırakılırsa,
  scheduler otomatik olarak `daily.yaml tracked_leagues` listesindeki ID’leri devralır.
- Böylece aynı listeyi 2 dosyada manuel tutma hatası ortadan kalkar.

Sezon kuralı:
- `static.yaml` içinde `params.season` boşsa, scheduler sadece güvenliyse infer eder:
  - daily.yaml’da top-level `season` varsa → onu kullanır
  - veya tracked_leagues içindeki tüm season değerleri aynıysa → onu kullanır
  - farklı season varsa → inference yapılmaz (prod safety). Bu durumda `params.season` explicit set edilmelidir.

### 3.7 `config/league_overrides.yaml`
- “Source name → league_id” gibi eşleştirme/override’lar.
- Amaç: lig hedeflerini (metin) doğru API league_id ile resolve etmek.

### 3.8 `config/resolved_tracked_leagues.yaml`
- Lig hedefleri + override’lar işlendiğinde oluşan “resolved list”.
- Prod’da “hangi ligleri takip ediyoruz?” sorusunun audit çıktısıdır.

---

## 4) “Yeni lig ekleme” prosedürü (prod-safe)

Kural: sistem kendisi rastgele lig eklemez; kontrol sende olmalı.

1) `config/jobs/daily.yaml` → `tracked_leagues` listesine yeni lig:
   - `id`
   - `season`
   - (opsiyonel) `name`

2) Redeploy.

3) İlk 1 saat MCP health:
   - `get_raw_error_summary(since_minutes=60)`
   - `get_daily_fixtures_by_date_status(since_minutes=180)`
   - `get_backfill_progress(job_id="fixtures_backfill_league_season")` (backfill ilerliyor mu?)

4) Eğer standings tarafında “missing teams” görürsen:
   - Dependency resolver bu boşluğu doldurur (gerekirse `/teams?id` fallback).
   - Eğer çok büyük çaplı değişim varsa, **tek seferlik bootstrap** uygulanır (bkz. Bölüm 5).

---

## 5) Bootstrap job’ları ne zaman açılır?

`bootstrap_leagues` ve `bootstrap_teams` “background sürekli çalışsın” diye tasarlanmadı.

Ne zaman açılır?
- Sezon rollover (ör. 2026) ve toplu update gerekiyorsa
- Yeni ligler eklendi ve toplu teams/leagues refresh isteniyorsa

Nasıl açılır? (tek seferlik)
- `config/jobs/static.yaml` içinde ilgili job `enabled: true`
- `params.season` gerektiğinde set edilir
- Redeploy → job bir kez çalışır → tekrar `enabled: false`

Not:
- Inheritance sayesinde `tracked_leagues` listesini ayrıca static.yaml’da yazmak zorunda kalmazsın.

---

## 5.1 Sezon geçişi (2025 → 2026) nasıl yönetilir?

Kural: `tracked_leagues[*].season` değerlerini topluca güncellemezsin. Liglerin sezon takvimi farklıdır.

### Ne zaman 2026’ya geçeceğini nasıl anlarsın?
Bu projede “tahmin” yok; kanıt var:
- Scheduler içindeki `season_rollover_watch` job’ı her gün `/leagues?season=<NEXT>` çağırır (1–3 request/day).
- Eğer tracked listendeki bir lig `next season` içinde görünmeye başlarsa log basar:
  - event: `season_rollover_available`
  - `league_id`, `current_season`, `next_season`
  - `action_file`: `config/jobs/daily.yaml`
  - `action_yaml_snippet`: kopyala-yapıştır YAML satırı

### Sen ne yapacaksın? (exact syntax)
`config/jobs/daily.yaml` içinde ilgili lig entry’sinde sadece season değişir:

```yaml
- id: 39
  name: Premier League
  season: 2026
```

Not:
- Bazı ligler 2026’ya geçtiğinde bile bazıları 2025 kalabilir (mix season normaldir).
- Eğer “eski sezonu da bir süre takip edeyim” ihtiyacın olursa, bunu tek entry ile değil, geçiş planı ile yönet (önce 2025’i stabilize et, sonra season bump).

---

## 6) Job cadence kuralları (prod modeli)

### 6.1 Live loop vs Daily fixtures
- Live loop (`/fixtures?live=all`, ~15s): canlı maç state’i
- Daily fixtures (`/fixtures?date=...`, ~30dk): günün fixtures listesini ve kapanışları toplu doğrular

### 6.2 Backfill job’ları
- Backfill tamamlandıktan sonra job’lar “no_work” olarak maliyetsiz döner.
- Bu, “boşluk olursa geri doldurma” emniyet kemeridir.

---

## 7) Minimum prod acceptance (MCP)

Deploy sonrası:
- `get_database_stats()`
- `get_rate_limit_status()`
- `get_raw_error_summary(since_minutes=60)`
- `get_backfill_progress()`
- `get_daily_fixtures_by_date_status(since_minutes=180)`

PASS:
- critical tool’lar exception üretmiyor
- 4xx/5xx/envelope error yok (veya anormal artmıyor)
- live loop ve daily cadence RAW’da kanıtlanıyor

---

## 9) “Bugün sistem gerçekten çalışıyor mu?” (Coolify terminal + kanıt)

Bu repo artık “cron’u beklemeden” job doğrulaması için yardımcı script’ler içerir:

### 9.1 Tek job’u 1 kere çalıştır (collector terminal)
- Top scorers (tek lig):
  - `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=top_scorers_daily python3 scripts/run_job_once.py`
- Team statistics (tek lig):
  - `cd /app && ONLY_LEAGUE_ID=39 JOB_ID=team_statistics_refresh python3 scripts/run_job_once.py`

### 9.2 DB kanıtı (postgres terminal)
Not: Postgres terminal bir shell’dir; SQL çalıştırmak için `psql` gerekir.

- RAW kanıtı:
  - `psql -U postgres -d api_football -c "SELECT COUNT(*) FROM raw.api_responses WHERE endpoint='/players/topscorers' AND fetched_at > NOW() - INTERVAL '1 hour';"`
- CORE kanıtı:
  - `psql -U postgres -d api_football -c "SELECT COUNT(*) FROM core.top_scorers;"`

### 9.3 Uçtan uca kontrol (collector terminal)
- `cd /app && sh scripts/e2e_validate.sh`

---

## 8) Değişmez kurallar (prod güvenlik)

- API çağrıları **GET only**
- Header **yalnızca** `x-apisports-key`
- Rate limit yok sayılmaz; token alınmadan request atılmaz
- DB’de zamanlar **UTC**
- CORE yazımları **UPSERT** (idempotent)
- Fixtures insert/upsert, **leagues + teams** dependency sağlanmadan yapılmaz


