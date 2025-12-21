## Lig Takip (Tracking) Kılavuzu — API-Football Collector

Bu doküman şunu netleştirir:

- “Lig eklemek” ne demek?
- **Canlı (live_loop)** ve **günlük (daily / per_tracked_leagues)** veri toplama ne kaydeder?
- “Maç bittiğinde” ne olur?
- Bir takım (örn. **Galatasaray**) kendi ligi dışında maç yaparsa ve o ligi eklemediysek **görebilir miyiz?**
- Nasıl doğrularız? (SQL + curl)

> Önemli: Bu sistem **config-driven** çalışır. “Kendisi rastgele lig eklemez.” Kontrol sizdedir.

---

## 0) Büyük resim: 5 temel bileşen

1) **Config**: `config/jobs/*.yaml`, `config/*.yaml`, ENV (Coolify)\n
2) **Collector**: Scheduler + job’lar (RAW→CORE→MART)\n
3) **Data Layers**: Postgres `raw`, `core`, `mart`\n
4) **MCP**: read-only izleme/sorgu tool’ları\n
5) **Operational**: loglar, rate limit, healthcheck, runbook, smoke test

---

## 1) “Lig eklemek” tam olarak neyi değiştirir?

Bu projede “lig eklemek” iki farklı yerde anlamlıdır:

### 1.1 Günlük/fixtures toplama için (fixtures + bülten)

- **Amaç**: Takip edilen liglerdeki maçlar “bültende var ama sistemde yok” olmasın.
- **Kaynak config**: `config/jobs/daily.yaml`
- **Kritik ayar**: `fixtures_fetch_mode: per_tracked_leagues`

Bu mod açıkken:
- Sistem `GET /fixtures?league=<id>&season=<season>&date=YYYY-MM-DD` ile **sadece tracked liglerin** o günkü fixtures’ını CORE’a alır.
- “Tracked değil” bir competition (kupa/UEFA vb) için fixtures görmek istiyorsanız **o competition da tracked** olmalı (veya ayrı bir global mod kullanılmalı).

> Not: Bu, “sezonun tamamını” otomatik getirir demek değildir. Sezonun tamamı için backfill gerekir (aşağıda).

### 1.2 Canlı maçları ekranda göstermek için (live loop)

- **Amaç**: `/v1/sse/live-scores` ve `mart.live_score_panel` içinde canlı maçlar görünsün.
- **Kaynak config**: `config/jobs/live.yaml -> filters.tracked_leagues`

Live loop şu API çağrısını yapar:
- `GET /fixtures?live=all` (15 saniyede bir)

Sonra **hangi liglerin** maçlarını CORE’a yazacağını `tracked_leagues` ile filtreler.

Örnek:
- UECL (UEFA Europa Conference League) = **848**
- Canlı panelde UECL görmek için `config/jobs/live.yaml` içine `- 848` eklenir.

---

## 2) Canlı (live_loop) ne kaydeder? Maç bitince ne olur?

### 2.1 Live loop ne yapar?

- `/fixtures?live=all` çeker
- Delta detector ile “değişen” maçları bulur (skor, status, elapsed)
- RAW’a arşivler (audit)
- CORE’da `core.fixtures` satırını **UPSERT** eder

### 2.2 Maç bitince (FT) ne olur?

Maç “FT” olduğunda:
- Live loop artık o maç “live” setinden düşer (API `live=all` artık dönmeyebilir).
- Ama CORE’daki kayıt **kalır** (silinmez).
- “Bitmiş maç” verisini görmek için canlı panel değil, **fixtures query** kullanılır.

### 2.3 Status kodları (en önemli olanlar)

- **NS**: Not Started (başlamadı)
- **1H**: First Half (1. devre)
- **HT**: Half Time (devre arası)
- **2H**: Second Half (2. devre)
- **FT**: Full Time (bitti)

`mart.live_score_panel` sadece canlı status’ları gösterir (1H/HT/2H vb). FT burada görünmez.

---

## 3) Daily / per_tracked_leagues ne kaydeder?

### 3.1 Per-league-by-date (fixtures_fetch_mode = per_tracked_leagues)

Günlük (TR 06:00 civarı, cron ile):
- `GET /fixtures?league=&season=&date=YYYY-MM-DD` çağrıları (tracked ligler için)
- RAW’a arşiv
- CORE’a `core.fixtures` UPSERT

#### 3.1.1 Bu mod neyi garanti eder, neyi etmez?

**Garanti ettiği şey (scope):**
- Tracked ligler için, günlük çalıştığı günün fixtures’ı **RAW+CORE’a girer**.

**Garanti etmediği şeyler:**
- Tracked olmayan competition’lar (kupa/UEFA vb) otomatik girmez.
- Sezonun tüm geçmişi otomatik girmez → **backfill** gerekir.

### 3.2 Backfill (sezonu geriye dönük tamamlama)

Bu repo, tracked ligler için **resumeable backfill** içerir:
- Job: `fixtures_backfill_league_season`
- İlerleme tablosu: `core.backfill_progress` (kaldığı yerden devam)
- Strateji: `/fixtures?league&season&from&to` ile 30 günlük pencereler (quota-safe)

Bu sayede:
- Yeni lig eklediğinizde, o ligin **current season** fixtures’ı gün gün tamamlanır.

### 3.2 Standings/Injuries gibi işler

Bu işler “lig+sezon” bazlıdır:
- `/standings?league=&season=`
- `/injuries?league=&season=`

Yani fixtures gibi “global date” kapsama yoktur. Eğer bir competition için standings/injuries istiyorsanız o competition’ı ayrıca takip kapsamına almak gerekebilir.

---

## 4) Kritik soru: Galatasaray başka ligde maç yaparsa ama biz o ligi eklemediysek görebilir miyiz?

### 4.1 Fixtures (maç listesi) açısından

Bu repo “tracked lig + backfill” modeliyle çalışıyorsa:
- O competition `tracked_leagues` listende yoksa, o maç **çekilmez** → CORE’da görünmez.
- Çözüm: ilgili competition’ı `tracked_leagues` listesine eklemek (sonra daily + backfill doldurur).

### 4.2 “Takım istatistikleri” açısından (genel kural)

Takımın “tüm sezon tüm maçları”nı görebilmek için iki şart gerekir:

1) O maçların fixtures’ının CORE’a alınmış olması (daily veya backfill)\n
2) İlgili detay endpoint’lerinin (events/players/statistics/lineups vb) işlenmiş olması (fixture_details job’ları)

Yani “lig eklemedik ama takım istatistikleri var mı?” sorusunun cevabı:
- **Maç CORE’a girdiyse**: evet, takımın o maçı görünür.\n
- **Maç CORE’a hiç girmediyse**: hayır.

> Bu yüzden bu modelde “kapsam” kontrolü `tracked_leagues` listesindedir (bilerek dar kapsam) ve “tüm sezon” için backfill kullanılır.

---

## 5) Doğrulama: takımın tüm maçlarını (tüm ligler) nasıl çekersin?

### 5.1 Takım id bul (isimle)

```sql
SELECT id, name
FROM core.teams
WHERE name ILIKE '%galatasaray%'
ORDER BY name
LIMIT 20;
```

### 5.2 Bir sezonda takımın tüm maçları (tüm ligler)

`<TEAM_ID>` ve `<SEASON>` değiştir:

```sql
SELECT
  f.id AS fixture_id,
  f.season,
  f.league_id,
  l.name AS league_name,
  f.date,
  f.status_short,
  th.name AS home_team,
  ta.name AS away_team,
  f.goals_home,
  f.goals_away
FROM core.fixtures f
JOIN core.leagues l ON l.id = f.league_id
JOIN core.teams th ON th.id = f.home_team_id
JOIN core.teams ta ON ta.id = f.away_team_id
WHERE f.season = <SEASON>
  AND (f.home_team_id = <TEAM_ID> OR f.away_team_id = <TEAM_ID>)
ORDER BY f.date ASC;
```

---

## 6) Read API ile dışarıdan nasıl erişirsin? (prod)

Prod domain:
- `https://readapi.zinalyze.pro`

### 6.1 Fixtures (tarih bazlı)

```bash
curl -sS -u "naneci:nanecigeliyor1." \
"https://readapi.zinalyze.pro/v1/fixtures?date=$(date -u +%F)&limit=50"
```

---

## 7) Yeni lig ekleme (tek başına yapılacak checklist)

Bu bölüm “yeni lig ekleyeceğim, nerelere dokunacağım?” sorusunun **tek kaynaktan** cevabı.

### 7.1 Hedefi seç (fixtures mı, live mı, details mı?)

- **Sadece fixtures (maç listesi) görünür olsun**:
  - Bu repoda fixtures ingest sadece **tracked ligler** içindir (`fixtures_fetch_mode: per_tracked_leagues`).
  - Sezon geçmişi için backfill gerekir (otomatik, resumeable).

- **Canlı panel/SSE’de görünsün**:
  - Live loop filtresi (aşağıdaki `config/jobs/live.yaml`) o ligi kapsamalı.

- **Maç detayları (players/events/statistics/lineups) gelsin + coverage düzgün olsun**:
  - Lig **tracked_leagues** içinde olmalı (fixture_details job’ları tracked-only çalışır).

### 7.2 Daily (tracked scope) → `config/jobs/daily.yaml`

Bu dosya “bizim takip ettiğimiz ligler”in ana kaynağıdır:

- `tracked_leagues` listesine ekle:
  - `id`: league_id
  - `name`: okunabilir isim
  - `season`: o lig için aktif sezon (örn. 2025)

Notlar:
- Bu liste **standings/injuries** gibi league+season job’larını da scope’lar.
- `fixture_details_recent_finalize` ve `fixture_details_backfill_90d` artık **sadece bu listeden** fixture seçer.

### 7.2.1 Bu repoda varsayılan cadence (daily-only, TR)
- Cron timezone: `SCHEDULER_TIMEZONE=Europe/Istanbul`
- `daily_fixtures_by_date`: TR 06:00
- `daily_standings`: TR 06:10
- `fixture_details_recent_finalize`: TR 06:30
- Backfill (gün içine yayılmış):\n
  - `fixtures_backfill_league_season`: her 10 dk (dakika 0/10/20/…)\n
  - `fixture_details_backfill_season`: her 10 dk (dakika 5/15/25/…) \n

### 7.2.2 Yeni lig ekleme prosedürü (prod-safe, minimum)
1) `config/jobs/daily.yaml -> tracked_leagues` listesine `{id, name, season}` ekle.\n
2) Deploy/redeploy.\n
3) Aynı gün tek seferlik (önerilen): `bootstrap_leagues` + `bootstrap_teams` çalıştır (seasons metadata + FK).\n
4) MCP ile izle:\n
   - `get_backfill_progress(job_id=\"fixtures_backfill_league_season\")` → completed oranı artmalı\n
   - `get_raw_error_summary(since_minutes=60)` → 429/5xx trendi olmamalı\n
   - `get_rate_limit_status()` → quota düşüşü kontrollü\n

### 7.3 Live (canlı filtre) → `config/jobs/live.yaml`

Canlı tarafta iki mod var:

- **Filtreli (önerilen, quota-safe)**:
  - `filters.tracked_leagues` içine league_id ekle.
  - Böylece `/fixtures?live=all` çağrısı yapılır ama CORE’a sadece bu ligler yazılır.

- **Filtresiz (track all)**:
  - `filters.tracked_leagues` boş bırakılırsa code “hepsini işle” anlamına gelir.
  - Bu **çok daha fazla CORE update** demektir; sadece gerçekten istiyorsan kullan.

### 7.4 Coverage /fixtures neden bazen “0%” görünür? → `config/coverage.yaml`

`/fixtures` coverage’ında iki seviye var:

- **Freshness + pipeline**: her ligde çalışır (beklenen fixture sayısı olmasa da).
- **Count coverage (sezon toplamı)**: sadece `expected_fixtures` içinde tanımlı liglerde anlamlıdır.

Yeni bir lig için sezon toplam fixture sayısını biliyorsan `config/coverage.yaml -> expected_fixtures` altına ekleyebilirsin.
Bilmiyorsan eklemek zorunda değilsin; sistem artık “beklenen yok → 0%” diye cezalandırmaz.

### 7.5 Static bootstrap (leagues/teams) → `config/jobs/static.yaml`

`bootstrap_leagues` ve `bootstrap_teams` job’ları kapalı olabilir.
Yeni lig ekledikten sonra şu senaryolarda açman gerekebilir:

- Yeni lig/teams henüz CORE’da yoksa (FK/dependency için)
- Yeni sezon rollover sonrası “ilk kez” takımlar/ligler güncellenecekse

Not: Bu projede bootstrap job’ları scope’u boşsa **daily.yaml tracked_leagues** üzerinden devralacak şekilde tasarlandı.

### 7.6 Deploy sonrası doğrulama (MCP)

- `get_fixture_detail_status(fixture_id=<tracked league + son 7 gün FT>)`
  - `has_players/events/statistics/lineups` true olmalı
- `get_coverage_status(league_id=<LID>, season=<SEASON>)`
  - `/fixtures/players|events|statistics|lineups` satırları gelmeli
  - `/fixtures` satırı freshness/pipeline ile anlamlı olmalı

### 6.2 Live scores (SSE)

```bash
curl -sS -u "naneci:nanecigeliyor1." \
"https://readapi.zinalyze.pro/v1/sse/live-scores?interval_seconds=3&limit=300"
```

> Not: Live SSE sadece “canlı” status’ları gösterir. FT (bitmiş) burada görünmez.

### 6.3 Smoke test (tek komut)

```bash
READ_API_BASE="https://readapi.zinalyze.pro" \
READ_API_BASIC_USER="naneci" \
READ_API_BASIC_PASSWORD="nanecigeliyor1." \
bash scripts/smoke_read_api.sh
```

---

## 8) Not: “Lig ekleme” için tek prosedür

Bu dokümanda “yeni lig ekleme” için güncel, prod-safe tek prosedür: **7.2.2**.\n
Live (opsiyonel) tarafı için: **7.3**.


