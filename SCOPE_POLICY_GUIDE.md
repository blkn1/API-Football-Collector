# Scope Policy Guide (Cup vs League) — Quota Optimization Without Losing Value

Bu dokümanın amacı: **“hangi veri hangi kapsamda?”** sorusuna tek yerden cevap vermek.

Scope policy ile hedef:
- **Quota israfını azaltmak** (ör. Cup’larda çoğu zaman boş/noisy olan `/standings` çağrılarını kesmek)
- Bunu yaparken **en değerli verileri asla kesmemek** (fixtures + fixture_details baseline)
- Monitoring’de “out-of-scope ≠ missing” ayrımını net yapmak

---

## 1) Nerede tanımlı?

- Config: `config/scope_policy.yaml`
- Enforcement (collector jobs):
  - `src/utils/standings.py` (`/standings`)
  - `src/jobs/team_statistics.py` (`/teams/statistics`)
  - `src/jobs/top_scorers.py` (`/players/topscorers`)
- Policy resolver: `src/utils/scope_policy.py`
- Görünürlük:
  - MCP tool: `get_scope_policy(league_id, season=None)`
  - MCP coverage: `get_coverage_status()` satırlarında `in_scope/scope_reason`
  - Read API ops: `GET /ops/api/scope_policy?league_id=...&season=...`

---

## 2) Temel kavramlar

### 2.1 “In-scope” ne demek?
Bir endpoint’in belirli `(league_id, season)` için **çalıştırılması** anlamına gelir.

### 2.2 “Out-of-scope” ne demek?
Endpoint’in o competition için **bilinçli olarak çalıştırılmaması** demektir.
- Bu durumda “veri eksik” diye incident açılmaz.
- Monitoring’de “out-of-scope” olarak işaretlenir.

### 2.3 Fail-open güvenlik kuralı
Eğer ligin type’ı (Cup/League) CORE’da bilinmiyorsa, policy **fail-open** davranır:
- Endpoint’i kapatmaz (in_scope=true)
- Amaç: yanlış metadata yüzünden değerli veriyi kesmemek.

---

## 3) Policy dosyası (`config/scope_policy.yaml`) yapısı

### 3.1 `version`
Policy değiştiğinde artırılır. Log ve MCP üzerinden hangi policy’nin uygulandığını görmek için kullanılır.

### 3.2 `baseline_enabled_endpoints`
Bu listede olan endpoint’ler **her zaman in-scope** kabul edilir.
Önerilen baseline (bu repo default):
- `/fixtures`
- `/fixtures/events`
- `/fixtures/lineups`
- `/fixtures/players`
- `/fixtures/statistics`
- `/injuries`

Bu baseline seçimi “kaliteyi koruma” garantisidir: en değerli datasetler burada.

### 3.3 `by_competition_type`
`core.leagues.type` değerine göre default kurallar.

- `League`:
  - Genellikle tablo ve sezon metrikleri anlamlı → `/standings`, `/teams/statistics`, `/players/topscorers` **enabled**
- `Cup`:
  - Knockout olduğu için çoğu zaman standings yoktur → yukarıdaki 3 endpoint **disabled**

> API-Football `core.leagues.type` tipik olarak `League` veya `Cup` döner.

### 3.4 `overrides`
Tek bir league/season için zorunlu davranış.

Örnek:
- “Bu Cup’ta standings var, çalışsın” → `enabled_endpoints: [/standings]`
- “Bu League’de standings gereksiz, çalışmasın” → `disabled_endpoints: [/standings]`

Override öncelik sırası:
1) baseline (always enabled)
2) overrides (force enable/disable)
3) type defaults (Cup/League)
4) type unknown → fail-open

---

## 4) Şu an sistemde hangi endpoint’ler policy ile yönetiliyor?

Policy şu üç endpoint’i quota optimizasyonu için kapsamdan çıkarabilir:
- `/standings`
- `/teams/statistics`
- `/players/topscorers`

Baseline olanlar policy ile kapatılmaz:
- `/fixtures` ve fixture_details fanout endpoint’leri
- `/injuries`

---

## 5) “Neden standings yok?” sorusunu nasıl cevaplarsın?

### 5.1 MCP ile (en net kanıt)
- `get_scope_policy(league_id=206)`  
  Beklenen: Cup ise `/standings in_scope=false` ve `reason=type_Cup_disabled` (veya override varsa `override_disabled`)

### 5.2 Read API ops ile (tek endpoint)
- `GET /ops/api/scope_policy?league_id=206`
  - MCP çıktısını proxy eder ve ops/debug kullanımına uygundur.

### 5.3 Coverage ile (out-of-scope ≠ missing ayrımı)
- `get_coverage_status(league_id=206)`
  - Coverage satırlarında:
    - `in_scope` (bool)
    - `scope_reason` (string)
    - `scope_policy_version` (int)

> Not: Coverage tablosu (mart.coverage_status) out-of-scope endpoint için satır üretmeyebilir; bu yüzden “neden yok?” sorusunun asıl cevabı `get_scope_policy` tool’udur.

---

## 6) Operasyonel doğrulama (redeploy sonrası)

1) MCP:
- `get_scope_policy(league_id=<LID>)`
2) RAW kanıtı:
- `get_raw_error_summary(since_minutes=60, endpoint="/standings")`
  - Cup’lar ağırlıklıysa request sayısında düşüş görülmeli.
3) Safety:
- `get_raw_error_summary(...)` içinde 4xx/5xx/envelope_errors artmamalı (policy sadece iş seçimi yapar; rate limiter/retry davranışını değiştirmez).

---

## 7) Ne zaman override yazmalıyız?

Override yazmak mantıklıdır eğer:
- API-Football o competition için gerçekten standings döndürüyor ve iş değeri varsa
- Bir ligde (League) top_scorers veya team_statistics gereksiz ve quota kısılmak isteniyorsa

Override yazmak risklidir eğer:
- League type yanlış ise (CORE metadata hatalı)
  - Bu durumda önce `core.leagues.type` doğrulanmalı (MCP: `get_league_info(league_id=...)`).


