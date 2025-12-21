## Read API Frontend Contract (React/Vite) — v1

Bu doküman, React/Vite ön yüzünün kullanacağı Read API endpoint’lerini **tek yerde** ve **frontend odaklı** anlatır.

Kapsam:
- Bugün/yarın/tarih bazlı fixture listesi
- Canlı sayfa (SSE)
- Takım sayfası: maç listesi + “last-20” özet metrikler + tek maç detay paketi

Kural:
- Tüm zamanlar **UTC** (frontend TR saatine çevirebilir).
- Read API **read-only**’dir; API-Football quota tüketmez.

---

## 1) Liste ekranları

### 1.1 Bugün fixtures (tek gün)
`GET /v1/fixtures?date=YYYY-MM-DD&league_id=&status=&limit=`

UI kullanımı:
- Tek çağrı ile bir günün tüm maçları alınır.
- Frontend bunu üç gruba böler:
  - **Live**: `status in {1H,2H,HT,ET,BT,P,LIVE,SUSP,INT}`
  - **Upcoming**: `status=NS` ve `date_utc > now()`
  - **Finished**: `status in {FT,AET,PEN}` + diğer final durumları

---

## 2) Canlı sayfa (SSE)

### 2.1 Live scores stream
`GET /v1/sse/live-scores?interval_seconds=3&limit=300`

- Source: `mart.live_score_panel`
- Not: Bu panel yalnızca canlı statüleri tutar; FT maçlar burada görünmez.

---

## 3) Takım sayfası (tüm turnuvalar)

### 3.1 Takım maç listesi (history + upcoming)
`GET /v1/teams/{team_id}/fixtures?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD&status=&limit=`

Amaç:
- Takımın lig/kupa/UEFA dahil maçlarını tarih aralığında getirmek.
- Ön yüzde “geçmiş maçlar” ve “gelecek maçlar” listesi.

Önerilen UI stratejisi:
- Son 90 gün: `from_date=today-90d`
- Gelecek 14 gün: `to_date=today+14d`
- Tek endpoint, tek list; UI `date_utc` ile sıralar ve böler.

### 3.2 Takım özet metrikleri (last-20)
`GET /v1/teams/{team_id}/metrics?last_n=20&as_of_date=YYYY-MM-DD`

Döndürülen alanlar (v1):
- `results`: W/D/L ve win rate
- `goals`: gf/ga, BTTS, clean sheet + home/away split
- `match_stats_avg`: (varsa) şut, isabetli şut, korner, kart, possession, offsides ortalamaları
- `fixtures_sample`: hesaplamaya giren son N fixture sample (debug)

Notlar:
- Hesaplama yalnızca tamamlanmış maçlarda yapılır: `FT/AET/PEN`
- Bazı liglerde “corners / cards / possession” gibi alanlar API’de eksik olabilir → değerler `null` dönebilir.

### 3.3 Tek maç detay paketi
`GET /v1/fixtures/{fixture_id}/details`

Amaç:
- Chart/analiz için tek fixture’ın detaylarını tek çağrıda almak.
- İçerik:
  - `events`
  - `statistics`
  - `lineups`
  - `players`

Kaynak:
- Öncelik: `core.fixture_details` JSONB snapshot
- Fallback: `core.fixture_events/statistics/lineups/players`

---

## 4) Head-to-head (H2H)
`GET /v1/h2h?home_team_id=&away_team_id=&limit=5`

Amaç:
- İki takımın son N karşılaşmasını göstermek + H2H tablosu oluşturmak.

---

## 5) Standings / injuries (opsiyonel ekranlar)
- `GET /v1/standings/{league_id}/{season}`
- `GET /v1/injuries?league_id=&season=&team_id=&player_id=&limit=`

---

## 6) Versiyonlama ve genişleme stratejisi

Bu contract intentionally “küçük ama güçlü” tutulur.
Sonraki fazlarda gerekirse eklenebilir:
- `/players` family (player profile/season totals)
- `/odds` family (market-aware features)
- Daha zengin takım metrikleri (first-goal time, penalty rates, comeback rates)

