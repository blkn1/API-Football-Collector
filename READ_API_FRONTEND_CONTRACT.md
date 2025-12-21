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

### 1.2 Frontend örnek akış (Today + Tomorrow + Status bucket)

Bu bölüm copy/paste amaçlıdır: React/Vite UI’de fixtures listesini nasıl çekeceğini ve nasıl böleceğini netleştirir.

#### 1) Status bucket kuralları (frontend tarafında)

```ts
export const LIVE_STATUSES = new Set(["1H","2H","HT","ET","BT","P","LIVE","SUSP","INT"]);
export const FINISHED_STATUSES = new Set(["FT","AET","PEN","AWD","WO","ABD","CANC"]);

export function bucketByStatus(status: string) {
  if (LIVE_STATUSES.has(status)) return "live";
  if (FINISHED_STATUSES.has(status)) return "finished";
  return "upcoming"; // NS, TBD, PST, vb.
}
```

Not:
- Canlı sayfa ayrı (SSE). Ama schedule sayfasında “live” bucket’ı göstermek istiyorsan bu set yeterli.

#### 2) UTC tarih üretimi (bugün/yarın)

```ts
export function utcYmd(d = new Date()) {
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export const utcToday = utcYmd();
export const utcTomorrow = utcYmd(new Date(Date.now() + 24 * 60 * 60 * 1000));
```

#### 3) Today + Tomorrow fetch pattern (2 request)

```ts
export async function fetchFixturesByDate(baseUrl: string, ymd: string) {
  const res = await fetch(`${baseUrl}/v1/fixtures?date=${ymd}&limit=200`, { credentials: "include" });
  if (!res.ok) throw new Error(`fixtures_fetch_failed:${res.status}`);
  return (await res.json()) as Array<{
    id: number;
    league_id: number;
    season: number | null;
    date_utc: string | null;
    status: string;
    home_team: string;
    away_team: string;
    goals_home: number | null;
    goals_away: number | null;
    updated_at_utc: string | null;
  }>;
}

export async function fetchTodayAndTomorrow(baseUrl: string) {
  const [today, tomorrow] = await Promise.all([
    fetchFixturesByDate(baseUrl, utcToday),
    fetchFixturesByDate(baseUrl, utcTomorrow),
  ]);
  return { today, tomorrow };
}
```

#### 4) Bugün 15:30 sonrası “başlamamış” liste (upcoming + kickoff>now)

```ts
export function upcomingLaterToday(fixtures: Array<{ status: string; date_utc: string | null }>) {
  const now = Date.now();
  return fixtures
    .filter(f => bucketByStatus(f.status) === "upcoming")
    .filter(f => f.date_utc && Date.parse(f.date_utc) > now)
    .sort((a, b) => Date.parse(a.date_utc ?? "") - Date.parse(b.date_utc ?? ""));
}
```

#### 5) Takvim ekranı (23/24 Aralık vb.)

Takvim sayfasında her gün için aynı çağrıyı yap:
- `GET /v1/fixtures?date=YYYY-MM-DD&limit=200`

Frontend “birden fazla gün” gösterecekse en basit yaklaşım:
- Görüntülenecek gün listesi oluştur (örn. [today..today+7])
- `Promise.all` ile her güne ayrı request at
- UI’de gün gün render et

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

