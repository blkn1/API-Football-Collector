## Read API Usage Guide (REST + SSE) — API-Football Collector

Bu doküman, prod’daki **Read API** servisinin nasıl kullanılacağını ve nasıl doğrulanacağını anlatır.

Read API amacı:
- **DB’deki (RAW/CORE/MART) verileri read-only** servis etmek
- n8n / dashboard / dış consumer’lar için “tek giriş” sağlamak

> Read API **yazma yapmaz**. INSERT/UPDATE yoktur.

---

## 1) Prod base URL

- Prod domain: `https://readapi.zinalyze.pro`

Shell’de kullanım için:

```bash
export READ_API_BASE="https://readapi.zinalyze.pro"
```

---

## 2) Access control (prod)

Read API prod’da **en az bir** güvenlik katmanı kullanmalıdır:

- **Basic Auth**:
  - `READ_API_BASIC_USER`
  - `READ_API_BASIC_PASSWORD`
- **IP allowlist** (ops):
  - `READ_API_IP_ALLOWLIST` (comma-separated)

Basic Auth varsa curl örneği:

```bash
curl -sS -u "USER:PASSWORD" "${READ_API_BASE}/v1/health"
```

---

## 3) REST endpoint’leri (read-only)

### 3.1 Health

```bash
curl -sS "${READ_API_BASE}/v1/health"
```

Beklenen:
- `ok=true`
- `db=true`

### 3.2 Quota

```bash
curl -sS "${READ_API_BASE}/v1/quota"
```

Beklenen:
- `daily_remaining` ve `minute_remaining` dolu (None olabilir; en son header yakalanmadıysa)

### 3.3 Fixtures

```bash
curl -sS "${READ_API_BASE}/v1/fixtures?date=2025-12-18&limit=50"
```

Filtreler:
- `league_id` (int)
- `date` (YYYY-MM-DD, UTC)
- `status` (ör: `NS`, `1H`, `HT`, `2H`, `FT`)
- `limit` (max 200)

### 3.3.1 Team Fixtures (tüm turnuvalar)

Bir takımın maçlarını (lig/kupa/UEFA dahil) tarih aralığında getirir:

```bash
curl -sS "${READ_API_BASE}/v1/teams/645/fixtures?from_date=2025-12-01&to_date=2025-12-31&limit=200"
```

Parametreler:
- `from_date` (YYYY-MM-DD, UTC) **zorunlu**
- `to_date` (YYYY-MM-DD, UTC) **zorunlu**
- `status` (opsiyonel: `NS`, `FT`, `1H` vb)
- `limit` (max 500)

### 3.3.2 Fixture Details (tek maç detay paketi)

Fixture detail tablolarını (events/statistics/lineups/players) tek çağrıda döner.\n
Not: Önce `core.fixture_details` JSONB snapshot tercih edilir; yoksa `core.fixture_*` normalize tablolardan fallback yapılır.

```bash
curl -sS "${READ_API_BASE}/v1/fixtures/1379134/details" | head -c 2000 && echo
```

### 3.3.3 Team Metrics (last-N=20) — tahmin feature set

Takımın son N tamamlanmış maçından (FT/AET/PEN) özet metrikleri hesaplar.\n
Frontend chart’ları için “hazır” ortalama/rate alanları içerir.

```bash
curl -sS "${READ_API_BASE}/v1/teams/645/metrics?last_n=20" | head -c 2000 && echo
```

Opsiyonel:
- `as_of_date=YYYY-MM-DD` → sadece bu tarihe kadar olan maçları dikkate alır.

### 3.3.4 Head-to-Head (H2H)

İki takımın son N karşılaşmasını döner:

```bash
curl -sS "${READ_API_BASE}/v1/h2h?home_team_id=645&away_team_id=610&limit=5"
```

### 3.4 Standings

```bash
curl -sS "${READ_API_BASE}/v1/standings/848/2025" | head
```

### 3.5 Teams

```bash
curl -sS "${READ_API_BASE}/v1/teams?search=Galatasaray&limit=20"
```

### 3.6 Injuries

```bash
curl -sS "${READ_API_BASE}/v1/injuries?league_id=203&season=2025&limit=50"
```

---

## 4) SSE endpoint’leri (read-only)

SSE bağlantıları “stream”dir; `curl` ile test edebilirsin.

### 4.1 System status (ops)

```bash
curl -sS "${READ_API_BASE}/v1/sse/system-status?interval_seconds=5"
```

### 4.2 Live scores

```bash
curl -sS "${READ_API_BASE}/v1/sse/live-scores?interval_seconds=3&limit=300"
```

Notlar:
- `mart.live_score_panel` bir **VIEW**’dur; live loop CORE’a yazdıkça burası güncellenir.
- View filtresi: `status_short` live statüler + `updated_at > now()-10 minutes`.

UEFA Europa Conference League (UECL) doğrulaması:
- UECL league_id = **848**
- DB kontrol:
  - `SELECT COUNT(*) FROM mart.live_score_panel WHERE league_id=848;`

---

## 5) Smoke test (otomatik)

Repo script’i:

```bash
READ_API_BASE="https://readapi.zinalyze.pro" bash scripts/smoke_read_api.sh
```

Basic Auth varsa:

```bash
READ_API_BASE="https://readapi.zinalyze.pro" \
READ_API_BASIC_USER="USER" \
READ_API_BASIC_PASSWORD="PASSWORD" \
bash scripts/smoke_read_api.sh
```

---

## 6) Sık sorunlar (troubleshooting)

### 6.1 `401 basic_auth_required` / `invalid_credentials`
- Basic Auth açıktır; doğru user/password ver.

### 6.2 `403 ip_not_allowed`
- `READ_API_IP_ALLOWLIST` aktif; istemci IP’si allowlist’te değil.

### 6.3 “Canlı yok” ama aslında var
Kontrol sırası:
1) RAW: `/fixtures?live=all` sonuç dönüyor mu?\n
2) `mart.live_score_panel` satır var mı?\n
3) Live loop `tracked_leagues` filtreli mi?\n
- UECL için `config/jobs/live.yaml` listesinde **848** olmalı (veya filter intentionally kaldırılmalı).


