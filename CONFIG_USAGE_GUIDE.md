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

### 3.5 `config/jobs/live.yaml`
- Scheduler içinde “live_loop job” tanımlı olsa bile, prod’da live loop ayrı servis olarak çalıştırılır.
- `ENABLE_LIVE_LOOP=1` ile live loop container aktif edilir.

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
   - `get_live_loop_status(since_minutes=5)` (live loop açıksa)

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
- `get_live_loop_status(since_minutes=5)` (ENABLE_LIVE_LOOP=1 ise)
- `get_daily_fixtures_by_date_status(since_minutes=180)`

PASS:
- critical tool’lar exception üretmiyor
- 4xx/5xx/envelope error yok (veya anormal artmıyor)
- live loop ve daily cadence RAW’da kanıtlanıyor

---

## 8) Değişmez kurallar (prod güvenlik)

- API çağrıları **GET only**
- Header **yalnızca** `x-apisports-key`
- Rate limit yok sayılmaz; token alınmadan request atılmaz
- DB’de zamanlar **UTC**
- CORE yazımları **UPSERT** (idempotent)
- Fixtures insert/upsert, **leagues + teams** dependency sağlanmadan yapılmaz


