## Lig Takip (Tracking) Kılavuzu — API-Football Collector

Bu doküman şunu netleştirir:

- “Lig eklemek” ne demek?
- **Canlı (live_loop)** ve **günlük (daily / global_by_date)** veri toplama ne kaydeder?
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

- **Amaç**: Maçlar “bültende var ama sistemde yok” olmasın.
- **Kaynak config**: `config/jobs/daily.yaml`
- **Kritik ayar**: `fixtures_fetch_mode: global_by_date`

Bu mod açıkken:
- Sistem `GET /fixtures?date=YYYY-MM-DD` ile **o gün oynanan tüm maçları** (kupalar/UEFA dahil) CORE’a alır.
- Yani “lig eklemedik → hiç göremeyiz” problemi **fixtures seviyesinde büyük ölçüde kalkar** (o gün kapsanır).

> Not: Bu, “sezonun tamamını” otomatik getirir demek değildir. Sadece **çektiğin günlerin** fixtures’ını getirir.

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

## 3) Daily / global_by_date ne kaydeder?

### 3.1 Global-by-date (fixtures_fetch_mode = global_by_date)

Her 30 dakikada bir:
- `GET /fixtures?date=YYYY-MM-DD` çağrısı
- RAW’a sayfa sayfa arşiv
- CORE’a `core.fixtures` UPSERT

Bu sayede:
- “tracked league değil” diye kaçan **kupa/UEFA** maçları bile o gün için CORE’a girer.

#### 3.1.1 Global-by-date neyi garanti eder, neyi etmez?

**Garanti ettiği şey (scope):**
- Sistem çalıştığı sürece, her 30 dakikada bir “bugünün UTC tarihi” için `/fixtures?date=YYYY-MM-DD` çekilir.
- O gün oynanan maçların fixtures’ı (API döndürdüğü kadar) **RAW+CORE’a girer**.

**Garanti etmediği şeyler (neden yine de “o maç hiç çekilmemiş” olabilir?):**
- **Geçmiş günler**: global_by_date sadece çalıştığı günleri kapsar. Sistem 10 gün kapalı kalırsa, o 10 günün fixtures’ı otomatik gelmez.\n
  - Çözüm: Backfill veya “kaçırılan günler” için manuel global date backfill (ileride ops).\n
- **UTC gün sınırı**: Bülten “TR günü” ise, TR 18 Aralık’ın bazı maçları UTC 17 Aralık’a düşebilir. Sen `date=2025-12-18` (UTC) sorgularsan o maç görünmez.\n
- **API’nin döndürmediği veriler**: API-Football o gün için bir competition’ı döndürmezse (nadir ama mümkündür), biz de çekemeyiz.\n
- **Kayıt anı**: Maç ileri tarihteyse, o günün date fetch’inde doğal olarak yoktur.\n

Özet:
- “Global-by-date açık → hiç eksik kalmaz” **sadece çalıştığın günler için** doğru bir hedeftir.
- “Tüm sezon %100” için backfill + izleme (MCP/coverage) gerekir.

### 3.2 Standings/Injuries gibi işler

Bu işler “lig+sezon” bazlıdır:
- `/standings?league=&season=`
- `/injuries?league=&season=`

Yani fixtures gibi “global date” kapsama yoktur. Eğer bir competition için standings/injuries istiyorsanız o competition’ı ayrıca takip kapsamına almak gerekebilir.

---

## 4) Kritik soru: Galatasaray başka ligde maç yaparsa ama biz o ligi eklemediysek görebilir miyiz?

### 4.1 Fixtures (maç listesi) açısından

**Eğer global_by_date aktifse**:
- Galatasaray’ın “o gün” oynadığı maç (kupa/UEFA dahil) **CORE’a girer**.
- Bu maçta Galatasaray takım id’si `home_team_id/away_team_id` olarak yer alır.

**Eğer global_by_date kapalı ve per_tracked_leagues ise**:
- O competition `tracked_leagues` listende yoksa, o maç **hiç çekilmeyebilir** → CORE’da görünmez.

### 4.2 “Takım istatistikleri” açısından (genel kural)

Takımın “tüm sezon tüm maçları”nı görebilmek için iki şart gerekir:

1) O maçların fixtures’ının CORE’a alınmış olması (daily/global veya backfill)\n
2) İlgili detay endpoint’lerinin (events/players/statistics/lineups vb) işlenmiş olması (fixture_details job’ları)

Yani “lig eklemedik ama takım istatistikleri var mı?” sorusunun cevabı:
- **Maç CORE’a girdiyse**: evet, takımın o maçı görünür.\n
- **Maç CORE’a hiç girmediyse**: hayır.

> Bu yüzden fixtures tarafında global_by_date “kapsam” problemine kalıcı çözüm getirir.

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

## 7) “Lig ekleme” pratik prosedürü (prod-safe)

### 7.1 Fixtures kapsamı (bülten gap)
- `config/jobs/daily.yaml`:
  - `fixtures_fetch_mode: global_by_date` (önerilen)

### 7.2 Live panel kapsamı (canlı göstermek)
- İlgili competition `league_id` bul:

```sql
SELECT id, name
FROM core.leagues
WHERE name ILIKE '%conference%' OR name ILIKE '%uefa%'
ORDER BY name;
```

- Sonra `config/jobs/live.yaml` içine ekle:
  - `filters.tracked_leagues: - <LEAGUE_ID>`

Örnek:
- UECL = **848**

### 7.3 Redeploy sonrası doğrulama
- DB:
  - `SELECT COUNT(*) FROM mart.live_score_panel WHERE league_id=<LEAGUE_ID>;`
- Read API:
  - `/v1/sse/live-scores`


