# API-Football Veri Kataloğu ve İstatistik Rehberi

> **Amaç:** Bu doküman, sistemimizde toplanan tüm futbol verilerini, Read API endpoint'lerini ve bunlarla yapılabilecek istatistikleri **proje yöneticisi seviyesinde** açıklar.

---

## 📊 Genel Bakış

Bu sistem, [API-Football](https://api-sports.io/documentation/football/v3) kaynaklı futbol verilerini:
1. **Toplar** (Collector servisi)
2. **Normalleştirir** (RAW → CORE → MART katmanları)
3. **Sunar** (Read API: REST + SSE)

**Önemli:** Read API **sadece okuma** yapar. Veri yazma/güncelleme işi Collector'a aittir.

---

## 🌐 Read API Endpoint'leri

| Endpoint | Açıklama | Örnek Kullanım |
|----------|----------|----------------|
| `GET /v1/health` | Sistem sağlık kontrolü | Servis ayakta mı? |
| `GET /v1/quota` | API-Football kota durumu | Günlük/dakika kalan çağrı |
| `GET /v1/fixtures` | Maç listesi (tarih/lig/durum filtreli) | Bugünün maçları |
| `GET /v1/fixtures/{id}/details` | Tek maçın detayları (olaylar, kadro, istatistik) | Maç analizi |
| `GET /v1/teams/{id}/fixtures` | Takımın maç geçmişi | Takım sayfası |
| `GET /v1/teams/{id}/metrics` | Takım özet metrikleri (son N maç) | Tahmin feature'ları |
| `GET /v1/h2h` | İki takım arası geçmiş maçlar | Karşılaşma geçmişi |
| `GET /v1/standings/{league_id}/{season}` | Lig puan durumu | Klasman tablosu |
| `GET /v1/teams` | Takım arama | Takım bul |
| `GET /v1/injuries` | Sakatlık listesi | Kadro planlaması |
| `GET /v1/sse/live-scores` | Canlı skor stream'i (SSE) | Canlı skor paneli |
| `GET /v1/sse/system-status` | Sistem durumu stream'i (SSE) | Ops monitör |

---

## 🗄️ Veritabanı Tabloları ve Alanları

### 1. `core.fixtures` — Maçlar (Ana Tablo)

**Ne tutulur:** Her futbol maçının temel bilgileri.

| Alan | Tip | Açıklama |
|------|-----|----------|
| `id` | BIGINT | API-Football maç ID'si (primary key) |
| `league_id` | BIGINT | Hangi ligde oynandı |
| `season` | INTEGER | Sezon yılı (örn: 2025) |
| `round` | TEXT | Hafta/tur bilgisi ("Regular Season - 18") |
| `date` | TIMESTAMPTZ | Maç tarihi ve saati (UTC) |
| `venue_id` | BIGINT | Stat ID'si |
| `home_team_id` | BIGINT | Ev sahibi takım ID |
| `away_team_id` | BIGINT | Deplasman takım ID |
| `status_short` | TEXT | Maç durumu kodu (aşağıda açıklandı) |
| `status_long` | TEXT | Maç durumu tam açıklama |
| `elapsed` | INTEGER | Oyun dakikası (canlı maçlar için) |
| `goals_home` | INTEGER | Ev sahibi gol sayısı |
| `goals_away` | INTEGER | Deplasman gol sayısı |
| `score` | JSONB | Devre skorları (halftime, fulltime, extratime, penalty) |
| `referee` | TEXT | Hakem adı |
| `updated_at` | TIMESTAMPTZ | Son güncelleme zamanı |

#### Maç Durumu Kodları (`status_short`)

| Kod | Anlam | Kategori |
|-----|-------|----------|
| `NS` | Not Started — Başlamadı | Upcoming |
| `1H` | First Half — İlk yarı | Live |
| `HT` | Half Time — Devre arası | Live |
| `2H` | Second Half — İkinci yarı | Live |
| `ET` | Extra Time — Uzatma | Live |
| `BT` | Break Time — Uzatma arası | Live |
| `P` | Penalty shootout — Penaltı serisi (oynanıyor) | Live |
| `SUSP` | Suspended — Askıya alındı | Live |
| `INT` | Interrupted — Kesintiye uğradı | Live |
| `FT` | Full Time — Normal süre bitti | Finished |
| `AET` | After Extra Time — Uzatmadan sonra | Finished |
| `PEN` | Penalty shootout bitti | Finished |
| `PST` | Postponed — Ertelendi | Diğer |
| `CANC` | Cancelled — İptal | Diğer |
| `ABD` | Abandoned — Yarıda kaldı | Diğer |
| `AWD` | Awarded — Hükmen | Diğer |
| `WO` | Walk Over | Diğer |
| `TBD` | To Be Defined — Belirsiz | Diğer |

---

### 2. `core.standings` — Puan Durumu

**Ne tutulur:** Her lig+sezon+takım kombinasyonu için güncel puan durumu.

| Alan | Tip | Açıklama |
|------|-----|----------|
| `league_id` | BIGINT | Lig ID |
| `season` | INTEGER | Sezon |
| `team_id` | BIGINT | Takım ID |
| `rank` | INTEGER | Sıralama (1, 2, 3...) |
| `points` | INTEGER | Toplam puan |
| `goals_diff` | INTEGER | Averaj (attığı - yediği) |
| `goals_for` | INTEGER | Attığı gol |
| `goals_against` | INTEGER | Yediği gol |
| `form` | TEXT | Son 5 maç formu ("WWDLW") |
| `status` | TEXT | Durum (same, up, down) |
| `description` | TEXT | Pozisyon açıklaması ("Champions League", "Relegation") |
| `group_name` | TEXT | Grup adı (varsa, örn: "Group A") |
| `all_stats` | JSONB | Tüm maç istatistikleri (played, win, draw, lose) |
| `home_stats` | JSONB | Ev sahibi maç istatistikleri |
| `away_stats` | JSONB | Deplasman maç istatistikleri |

#### `all_stats` / `home_stats` / `away_stats` JSONB Yapısı:
```json
{
  "played": 18,
  "win": 12,
  "draw": 3,
  "lose": 3,
  "goals": {
    "for": 35,
    "against": 15
  }
}
```

---

### 3. `core.fixture_statistics` — Maç İstatistikleri (Takım Bazlı)

**Ne tutulur:** Her maçtaki takım istatistikleri.

| Alan | Tip | Açıklama |
|------|-----|----------|
| `fixture_id` | BIGINT | Maç ID |
| `team_id` | BIGINT | Takım ID |
| `statistics` | JSONB | İstatistik listesi |

#### `statistics` JSONB Yapısı (Örnek):
```json
[
  {"type": "Shots on Goal", "value": 7},
  {"type": "Shots off Goal", "value": 5},
  {"type": "Total Shots", "value": 15},
  {"type": "Blocked Shots", "value": 3},
  {"type": "Shots insidebox", "value": 10},
  {"type": "Shots outsidebox", "value": 5},
  {"type": "Fouls", "value": 12},
  {"type": "Corner Kicks", "value": 6},
  {"type": "Offsides", "value": 2},
  {"type": "Ball Possession", "value": "55%"},
  {"type": "Yellow Cards", "value": 2},
  {"type": "Red Cards", "value": 0},
  {"type": "Goalkeeper Saves", "value": 4},
  {"type": "Total passes", "value": 450},
  {"type": "Passes accurate", "value": 380},
  {"type": "Passes %", "value": "84%"},
  {"type": "expected_goals", "value": "1.75"}
]
```

#### Mevcut İstatistik Türleri (Liglere Göre Değişir):

| İstatistik | Açıklama | Birim |
|------------|----------|-------|
| `Total Shots` | Toplam şut | Sayı |
| `Shots on Goal` | İsabetli şut | Sayı |
| `Shots off Goal` | İsabetsiz şut | Sayı |
| `Blocked Shots` | Bloke edilen şut | Sayı |
| `Shots insidebox` | Ceza sahası içi şut | Sayı |
| `Shots outsidebox` | Ceza sahası dışı şut | Sayı |
| `Corner Kicks` | Korner | Sayı |
| `Offsides` | Ofsayt | Sayı |
| `Ball Possession` | Top hakimiyeti | Yüzde (%) |
| `Fouls` | Faul | Sayı |
| `Yellow Cards` | Sarı kart | Sayı |
| `Red Cards` | Kırmızı kart | Sayı |
| `Goalkeeper Saves` | Kaleci kurtarışı | Sayı |
| `Total passes` | Toplam pas | Sayı |
| `Passes accurate` | İsabetli pas | Sayı |
| `Passes %` | Pas isabeti | Yüzde (%) |
| `expected_goals` | Beklenen gol (xG) | Ondalık |

> ⚠️ **Not:** Bazı liglerde (özellikle alt ligler) tüm istatistikler mevcut olmayabilir. API boş veya `null` dönebilir.

---

### 4. `core.fixture_events` — Maç Olayları

**Ne tutulur:** Maç içi olaylar (goller, kartlar, değişiklikler).

| Alan | Tip | Açıklama |
|------|-----|----------|
| `fixture_id` | BIGINT | Maç ID |
| `event_key` | TEXT | Olayın benzersiz anahtarı |
| `time_elapsed` | INTEGER | Dakika |
| `time_extra` | INTEGER | Uzatma dakikası (45+2 → elapsed=45, extra=2) |
| `team_id` | BIGINT | Olayı yapan takım |
| `player_id` | BIGINT | Olayı yapan oyuncu |
| `assist_id` | BIGINT | Asist yapan oyuncu (varsa) |
| `type` | TEXT | Olay tipi |
| `detail` | TEXT | Olay detayı |
| `comments` | TEXT | Yorum (varsa) |

#### Olay Tipleri (`type`):

| Tip | Açıklama | Detay Örnekleri |
|-----|----------|-----------------|
| `Goal` | Gol | Normal Goal, Own Goal, Penalty |
| `Card` | Kart | Yellow Card, Red Card, Second Yellow card |
| `subst` | Oyuncu değişikliği | Substitution 1, 2, 3... |
| `Var` | VAR kararı | Goal cancelled, Penalty confirmed |

---

### 5. `core.fixture_lineups` — Kadrolar

**Ne tutulur:** Maç başlama kadroları ve yedekler.

| Alan | Tip | Açıklama |
|------|-----|----------|
| `fixture_id` | BIGINT | Maç ID |
| `team_id` | BIGINT | Takım ID |
| `formation` | TEXT | Diziliş ("4-3-3", "4-4-2") |
| `start_xi` | JSONB | İlk 11 oyuncu listesi |
| `substitutes` | JSONB | Yedek oyuncular |
| `coach` | JSONB | Teknik direktör bilgisi |
| `colors` | JSONB | Forma renkleri |

#### `start_xi` / `substitutes` Yapısı:
```json
[
  {
    "player": {
      "id": 12345,
      "name": "M. Salah",
      "number": 11,
      "pos": "F",
      "grid": "1:1"
    }
  }
]
```

---

### 6. `core.fixture_players` — Oyuncu Maç Performansları

**Ne tutulur:** Her oyuncunun o maçtaki performans istatistikleri.

| Alan | Tip | Açıklama |
|------|-----|----------|
| `fixture_id` | BIGINT | Maç ID |
| `team_id` | BIGINT | Takım ID |
| `player_id` | BIGINT | Oyuncu ID |
| `player_name` | TEXT | Oyuncu adı |
| `statistics` | JSONB | Performans istatistikleri |

#### `statistics` JSONB Yapısı (Oyuncu Seviyesi):
```json
{
  "games": {
    "minutes": 90,
    "number": 11,
    "position": "F",
    "rating": "8.2",
    "captain": false,
    "substitute": false
  },
  "offsides": 1,
  "shots": {
    "total": 4,
    "on": 3
  },
  "goals": {
    "total": 1,
    "conceded": 0,
    "assists": 1,
    "saves": null
  },
  "passes": {
    "total": 35,
    "key": 3,
    "accuracy": "85"
  },
  "tackles": {
    "total": 2,
    "blocks": 0,
    "interceptions": 1
  },
  "duels": {
    "total": 12,
    "won": 8
  },
  "dribbles": {
    "attempts": 5,
    "success": 3,
    "past": null
  },
  "fouls": {
    "drawn": 2,
    "committed": 1
  },
  "cards": {
    "yellow": 0,
    "red": 0
  },
  "penalty": {
    "won": null,
    "commited": null,
    "scored": 0,
    "missed": 0,
    "saved": null
  }
}
```

---

### 7. `core.teams` — Takımlar

| Alan | Tip | Açıklama |
|------|-----|----------|
| `id` | BIGINT | Takım ID (API-Football) |
| `name` | TEXT | Takım adı |
| `code` | TEXT | Kısa kod (GS, FB, BJK) |
| `country` | TEXT | Ülke |
| `founded` | INTEGER | Kuruluş yılı |
| `national` | BOOLEAN | Milli takım mı? |
| `logo` | TEXT | Logo URL |
| `venue_id` | BIGINT | Stat ID |

---

### 8. `core.leagues` — Ligler

| Alan | Tip | Açıklama |
|------|-----|----------|
| `id` | BIGINT | Lig ID |
| `name` | TEXT | Lig adı |
| `type` | TEXT | Tip (League, Cup) |
| `logo` | TEXT | Logo URL |
| `country_name` | TEXT | Ülke adı |
| `country_code` | TEXT | Ülke kodu (TR, GB) |
| `seasons` | JSONB | Sezon bilgileri ve coverage metadata |

---

### 9. `core.venues` — Stadyumlar

| Alan | Tip | Açıklama |
|------|-----|----------|
| `id` | BIGINT | Stat ID |
| `name` | TEXT | Stat adı |
| `address` | TEXT | Adres |
| `city` | TEXT | Şehir |
| `country` | TEXT | Ülke |
| `capacity` | INTEGER | Kapasite |
| `surface` | TEXT | Zemin tipi (grass, artificial turf) |
| `image` | TEXT | Görsel URL |

---

### 10. `core.injuries` — Sakatlıklar

| Alan | Tip | Açıklama |
|------|-----|----------|
| `league_id` | BIGINT | Lig ID |
| `season` | INTEGER | Sezon |
| `team_id` | BIGINT | Takım ID |
| `player_id` | BIGINT | Oyuncu ID |
| `player_name` | TEXT | Oyuncu adı |
| `team_name` | TEXT | Takım adı |
| `type` | TEXT | Sakatlık tipi (Missing Fixture, Questionable) |
| `reason` | TEXT | Sebep (Knee Injury, Suspended) |
| `severity` | TEXT | Şiddet |
| `date` | DATE | Tarih |

---

### 11. `core.players` — Oyuncular

| Alan | Tip | Açıklama |
|------|-----|----------|
| `id` | BIGINT | Oyuncu ID |
| `name` | TEXT | Tam ad |
| `firstname` | TEXT | Ad |
| `lastname` | TEXT | Soyad |
| `age` | INTEGER | Yaş |
| `birth_date` | DATE | Doğum tarihi |
| `nationality` | TEXT | Uyruk |
| `height` | TEXT | Boy (180 cm) |
| `weight` | TEXT | Kilo (75 kg) |
| `injured` | BOOLEAN | Sakat mı? |
| `photo` | TEXT | Fotoğraf URL |

---

## 📈 İstatistik Potansiyeli ve Kullanım Senaryoları

### ✅ Şu An Yapılabilenler

#### 1. **Takım Formu Analizi**
```
Endpoint: GET /v1/teams/{team_id}/metrics?last_n=20
```
- Son 20 maçta galibiyet/beraberlik/mağlubiyet
- Gol ortalaması (attığı/yediği)
- BTTS (İki Takım da Gol Atar) oranı
- Clean Sheet (gol yememe) oranı
- Ev/deplasman ayrımı

#### 2. **Maç Öncesi Analiz**
```
Endpoint: GET /v1/h2h?home_team_id=X&away_team_id=Y
Endpoint: GET /v1/teams/{id}/metrics
```
- İki takım arası son 5-10 maç geçmişi
- Her iki takımın son form durumu
- Karşılaşmalardaki gol trendi

#### 3. **Canlı Skor Takibi**
```
Endpoint: GET /v1/sse/live-scores
```
- Anlık skor güncellemeleri
- Maç dakikası
- Son 10 dakika içinde güncellenen maçlar

#### 4. **Lig Puan Durumu**
```
Endpoint: GET /v1/standings/{league_id}/{season}
```
- Güncel sıralama
- Puan, averaj, form
- Şampiyon/küme düşme bölgeleri

#### 5. **Maç Detayları**
```
Endpoint: GET /v1/fixtures/{fixture_id}/details
```
- Gol dakikaları ve atan oyuncular
- Kart bilgileri
- Oyuncu değişiklikleri
- Takım istatistikleri (şut, korner, top hakimiyeti)
- Kadro ve diziliş

#### 6. **Sakatlık Takibi**
```
Endpoint: GET /v1/injuries?team_id=X
```
- Takımın sakat oyuncuları
- Sakatlık sebebi
- Maç kadrosunda olup olmama durumu

---

### 📊 Örnek İstatistik Hesaplamaları

#### 1. **BTTS (Both Teams To Score) Oranı**
```
Hesaplama: (Her iki takımın da gol attığı maç sayısı / Toplam maç) × 100
Kaynak: core.fixtures → goals_home > 0 AND goals_away > 0
```

#### 2. **Over/Under 2.5 Gol**
```
Over 2.5: (goals_home + goals_away) > 2.5
Under 2.5: (goals_home + goals_away) <= 2.5
Kaynak: core.fixtures
```

#### 3. **Takım Galibiyet Oranı**
```
Galibiyet sayısı / Toplam maç × 100
Kaynak: core.fixtures veya core.standings (all_stats.win / all_stats.played)
```

#### 4. **Korner Ortalaması**
```
Hesaplama: SUM(corner_kicks) / Maç sayısı
Kaynak: core.fixture_statistics → statistics[type='Corner Kicks']
```

#### 5. **İlk Yarı / İkinci Yarı Gol Dağılımı**
```
Kaynak: core.fixtures → score JSONB (halftime.home, halftime.away, fulltime.home, fulltime.away)
İlk yarı golleri = halftime skorları
İkinci yarı golleri = fulltime - halftime
```

---

### ⚠️ Şu An Toplanmayan / Eksik Veriler

| Veri | Durum | Not |
|------|-------|-----|
| **Odds (Bahis Oranları)** | ❌ Toplanmıyor | API-Football'dan çekilebilir ama şu an aktif değil |
| **Oyuncu Sezon Toplamları** | ⚠️ Kısmi | fixture_players var ama sezon toplamı yok |
| **Takım Sezon İstatistikleri** | ❌ Toplanmıyor | `/teams/statistics` endpoint'i kullanılmıyor |
| **Transfer Verileri** | ❌ Yok | API-Football'da var ama toplanmıyor |
| **xG (Expected Goals)** | ⚠️ Kısmi | Bazı liglerde fixture_statistics'te var |
| **Heatmap / Pozisyon Verisi** | ❌ Yok | API-Football'da yok |

---

## 🔄 Veri Güncelleme Sıklığı

| Veri Tipi | Güncelleme Sıklığı |
|-----------|-------------------|
| Canlı maç skorları | 15 saniye (live loop) |
| Günlük maç listesi | 30 dakika |
| Maç detayları (biten maçlar) | 10-15 dakika |
| Puan durumu | Günde 1 + backfill |
| Sakatlıklar | Saatte 1 |
| Takım/Lig bilgileri | Haftalık (bootstrap) |

---

## 🎯 Tracked Leagues (İzlenen Ligler)

Sistem şu an **83+ lig** izliyor. Öne çıkanlar:

| ID | Lig | Ülke |
|----|-----|------|
| 39 | Premier League | İngiltere |
| 140 | La Liga | İspanya |
| 78 | Bundesliga | Almanya |
| 135 | Serie A | İtalya |
| 61 | Ligue 1 | Fransa |
| 203 | Süper Lig | Türkiye |
| 204 | 1. Lig | Türkiye |
| 2 | UEFA Şampiyonlar Ligi | Avrupa |
| 848 | UEFA Konferans Ligi | Avrupa |

Tam liste: `config/jobs/daily.yaml` → `tracked_leagues`

---

## 🔐 API Erişimi

### Production URL
```
https://readapi.zinalyze.pro
```

### Authentication
- **Basic Auth** (user/password) veya
- **IP Allowlist** (sadece belirli IP'ler)

### Rate Limit
- Read API'nin kendi limiti yok (sadece DB sorgusu)
- Upstream API-Football: 7500/gün, ~300/dakika

---

## 📝 Özet Tablo: Ne Var, Ne Yok?

| Özellik | Durum | Kaynak |
|---------|-------|--------|
| Maç listesi (tarih/lig) | ✅ Var | `/v1/fixtures` |
| Canlı skorlar | ✅ Var | `/v1/sse/live-scores` |
| Maç detayları | ✅ Var | `/v1/fixtures/{id}/details` |
| Takım maçları | ✅ Var | `/v1/teams/{id}/fixtures` |
| Takım metrikleri | ✅ Var | `/v1/teams/{id}/metrics` |
| H2H geçmişi | ✅ Var | `/v1/h2h` |
| Puan durumu | ✅ Var | `/v1/standings` |
| Sakatlıklar | ✅ Var | `/v1/injuries` |
| Korner/kart istatistikleri | ✅ Var | fixture_statistics JSONB |
| Oyuncu performansları | ✅ Var | fixture_players JSONB |
| xG (Expected Goals) | ⚠️ Kısmi | Bazı liglerde var |
| Bahis oranları | ❌ Yok | Toplanmıyor |
| Transfer verileri | ❌ Yok | Toplanmıyor |
| Oyuncu sezon toplamları | ❌ Yok | Aggregation gerekli |

---

## 📞 Teknik Destek

Bu dokümanla ilgili sorularınız için:
- **Teknik:** Cursor AI (bu asistan) veya development ekibi
- **Veri kalitesi:** MCP tool'ları (`get_coverage_status`, `get_database_stats`)
- **API durumu:** `GET /v1/health` + `GET /v1/quota`

---

*Son güncelleme: 2025-12-21*

