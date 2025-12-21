## AI Editor Spec — React/Vite Frontend (Read API v1)

Bu doküman, bir yapay zeka editörünün (Cursor/Claude vs.) **tek seferde doğru** bir React/Vite frontend geliştirebilmesi için “tek kaynak gerçek” olarak kullanılmalıdır.

Kapsam:
- Daily fixtures (today/tomorrow) + status bucket (live/upcoming/finished)
- Live page (SSE)
- Team page (fixtures + metrics + fixture details + h2h)
- Güvenlik: frontend **secret tutmaz**

Referans dokümanlar:
- `READ_API_FRONTEND_CONTRACT.md`
- `READ_API_USAGE_GUIDE.md`

---

## 1) Kesin kurallar (değişmez)

- **UTC**: API’den gelen tüm tarih/saatler UTC’dir. Frontend sadece gösterim için locale çevirir.
- **Secret yok**: Frontend repo/image içine Basic Auth user/pass, API key veya başka secret **gömülmez**.
- **Tracked leagues zorunlu**: `/v1/fixtures` ve `/v1/sse/live-scores` çıktısı tüm ligleri içerebilir. “İddaa benzeri” UI için `league_id` whitelist filtre uygulanır.
- **Status bucket setleri sabit**:
  - `LIVE_STATUSES = {"1H","2H","HT","ET","BT","P","LIVE","SUSP","INT"}`
  - `FINISHED_STATUSES = {"FT","AET","PEN","AWD","WO","ABD","CANC"}`
  - Diğer tüm statüler → upcoming

---

## 2) Environment (frontend)

- `VITE_API_BASE="/api"` (same-origin reverse proxy)
- `VITE_TRACKED_LEAGUES="203,39,140,78,135,2"`

Not:
- `VITE_TRACKED_LEAGUES` boşsa “show all” yerine **boş liste** göstermek daha güvenlidir (yanlış/çöp lig spam’ini engeller). UI’de “tracked leagues tanımlı değil” uyarısı göster.

---

## 3) Endpoint’ler (Read API v1)

Frontend sadece bu endpoint’leri kullanır:
- `GET /v1/health`
- `GET /v1/fixtures?date=YYYY-MM-DD&league_id=&status=&limit=`
- `GET /v1/sse/live-scores?interval_seconds=3&limit=300`
- `GET /v1/teams/{team_id}/fixtures?from_date=YYYY-MM-DD&to_date=YYYY-MM-DD&status=&limit=`
- `GET /v1/teams/{team_id}/metrics?last_n=20&as_of_date=YYYY-MM-DD`
- `GET /v1/fixtures/{fixture_id}/details`
- `GET /v1/h2h?home_team_id=&away_team_id=&limit=5`

---

## 4) UI sayfaları (minimum)

- **Daily Fixtures**
  - Today + Tomorrow: iki request (`date=todayUTC`, `date=tomorrowUTC`)
  - Listeyi `trackedLeagues` ile filtrele
  - Tab’ler: Live / Upcoming / Finished
- **Live**
  - SSE ile live-score akışı
  - Gelen event’i parse et, `trackedLeagues` ile filtrele, tablo/kart listesi güncelle
  - SSE hata olursa: kullanıcıya “canlı bağlantı koptu” uyarısı + manuel retry
- **Team**
  - `/teams/{id}/fixtures` (last 90d + next 14d gibi aralık)
  - `/teams/{id}/metrics?last_n=20`
  - Fixture card’dan `/fixtures/{fixture_id}/details`
  - H2H: `/v1/h2h?home_team_id=&away_team_id=`

---

## 5) League name (league_id → label)

Read API v1’de `/v1/leagues` yok. Frontend tracked leagues ile sınırlı statik map kullanır:

```ts
export const LEAGUE_NAMES: Record<number, string> = {
  203: "Trendyol Süper Lig",
  39: "Premier League",
  140: "La Liga",
  78: "Bundesliga",
  135: "Serie A",
  2: "UEFA Şampiyonlar Ligi",
};
```

---

## 6) Reverse proxy (same-origin /api)

Tarayıcıdan `https://readapi...` origin’ine direkt çağrı CORS nedeniyle çalışmayabilir. Bu yüzden frontend **same-origin** `/api` path’i üzerinden konuşur.

Nginx örneği (auth injection yok):

```nginx
location /api/ {
  rewrite ^/api/(.*) /$1 break;
  proxy_pass https://readapi.zinalyze.pro;
  proxy_set_header Host readapi.zinalyze.pro;
  proxy_buffering off;     # SSE için gerekli
  proxy_cache off;
  proxy_read_timeout 3600s;
  proxy_http_version 1.1;
}
```

---

## 7) Copy/paste prompt (AI editör için)

```text
Sen kıdemli bir React/TypeScript mühendisisin. React+Vite+TS ile bir “FootData” frontend geliştir.

Kesin kurallar:
- Read API v1 endpointleri dışında endpoint yok.
- Tüm zamanlar UTC.
- VITE_TRACKED_LEAGUES whitelist zorunlu: fixtures + SSE akışını league_id ile filtrele.
- Secret yok: Basic Auth/user/pass veya API key asla frontend’e yazılmayacak.
- Status bucket setleri sabit:
  LIVE_STATUSES={"1H","2H","HT","ET","BT","P","LIVE","SUSP","INT"}
  FINISHED_STATUSES={"FT","AET","PEN","AWD","WO","ABD","CANC"}

İstenen sayfalar:
- Daily fixtures (today/tomorrow) + tabs (live/upcoming/finished)
- Live page (SSE) + reconnect UX
- Team page (fixtures + metrics + fixture details + h2h)

Networking:
- base URL: import.meta.env.VITE_API_BASE || "/api"
- fetch hatalarında kullanıcıya anlaşılır mesaj göster, retry butonu koy.

UI:
- Basit, temiz, responsive.
- Fixture card: league name (statik map), teams, score/time, status, updated_at.

Çıktı:
- Dosya/dizin yapısı
- Örnek componentler
- `src/lib/api.ts` (typed fetch wrappers)
- `src/lib/trackedLeagues.ts` (parser + filter helpers)
```


