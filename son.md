# API Football Collector - Proje Geliştirme Özeti

Bu belge, cursor_son.md dosyasında belgelenen Cursor AI ile yapılan tüm geliştirmeleri özetlemektedir.

---

## 1. Config Katmanı Oluşturulması

### src/utils/config.py
**Yeni dosya oluşturuldu:**
- `load_api_config()` fonksiyonu: config/api.yaml dosyasından API ayarlarını yükler
- `load_rate_limiter_config()` fonksiyonu: config/rate_limiter.yaml dosyasından rate limiter ayarlarını yükler
- `load_yaml()` yardımcı fonksiyonu
- Env variable desteği (API_FOOTBALL_API_CONFIG, API_FOOTBALL_RATE_LIMITER_CONFIG)
- APIConfig ve RateLimiterConfig dataclass'ları

**Amaç:** Hard-coded değerler yerine config-driven yaklaşım

---

## 2. Rate Limiter Geliştirmeleri

### src/collector/rate_limiter.py
**Eklenen özellikler:**
- `EmergencyStopError` exception sınıfı: Günlük quota tehlikeli derecede düşükse sistem durdurulur
- `emergency_stop_threshold` parametresi RateLimiter constructor'ına eklendi
- `update_from_headers()` metodu: API'den gelen quota header'larını parse eder (x-ratelimit-requests-remaining, X-RateLimit-Remaining)
- `_raise_if_emergency_stop_locked()` internal metodu: Threshold kontrolü yapar
- `quota` property: QuotaSnapshot döndürür
- Token clamping: API'nin bildirdiği minute remaining değerine göre local token sayısı düzeltilir

**Amaç:** Production ortamında API quota tükenmesini önlemek ve gerçek zamanlı quota takibi

---

## 3. Scripts Config-Driven Hale Getirildi

### scripts/daily_sync.py
**Değişiklikler:**
- Hard-coded season=2024 default değeri kaldırıldı
- Hard-coded TRACKED_LEAGUES_DEFAULT listesi kaldırıldı
- `_load_daily_config()` fonksiyonu eklendi: config/jobs/daily.yaml'dan season ve tracked_leagues okur
- Config yoksa hata fırlatır (varsayım yapmaz)
- `load_api_config()` ve `load_rate_limiter_config()` kullanımı eklendi
- EmergencyStopError yakalama eklendi

### scripts/live_loop.py
**Değişiklikler:**
- Hard-coded tracked leagues listesi kaldırıldı
- `_load_tracked_leagues_from_config()` fonksiyonu eklendi: config/jobs/live.yaml'dan okur
- Config boşsa hata fırlatır
- EmergencyStopError ve RateLimitError yakalama iyileştirildi
- Exponential backoff mekanizması eklendi

### scripts/bootstrap.py
**Değişiklikler:**
- `_load_bootstrap_plan_from_static_config()` fonksiyonu eklendi
- config/jobs/static.yaml'dan season ve tracked_leagues okur
- Hard-coded default değerler kaldırıldı
- `load_api_config()` ve `load_rate_limiter_config()` kullanımı eklendi
- EmergencyStopError yakalama eklendi

**Amaç:** Konfigürasyon dosyalarına bağlı, tahmin yapmayan (no assumption) production-ready kod

---

## 4. MCP Server Oluşturulması

### src/mcp/server.py
**Yeni dosya - FastMCP ile MCP server:**

**Eklenen Tools:**
1. `get_coverage_status(league_id, season)`: Mart coverage metriklerini döndürür
2. `get_coverage_summary(season)`: Season için coverage özeti
3. `get_rate_limit_status()`: API quota durumunu döndürür
4. `get_last_sync_time(endpoint)`: Son RAW fetch zamanı
5. `query_fixtures(league_id, date, status, limit)`: Fixture sorgulama
6. `query_standings(league_id, season)`: Standings sorgulama
7. `query_teams(league_id, search, limit)`: Team sorgulama
8. `get_league_info(league_id)`: League bilgisi
9. `get_database_stats()`: DB istatistikleri (record count, son aktivite)
10. `list_tracked_leagues()`: Tracked leagues listesi
11. `get_job_status(job_name)`: Job durumu (config + log parse)

**Özellikler:**
- Async DB query'leri (asyncio.to_thread ile)
- Config dosyalarından season okuma
- Season yoksa hata döndürür (2024 fallback kaldırıldı)
- Structlog JSONL parse etme (logs/collector.jsonl)
- transport="stdio" (Claude Desktop entegrasyonu için)

**Amaç:** Claude Desktop'tan projeyi izleme ve sorgulama yeteneği

---

## 5. Docker ve Deployment

### Dockerfile
**Yeni dosya - root dizinde:**
```dockerfile
FROM python:3.11-slim
- PYTHONDONTWRITEBYTECODE=1
- PYTHONUNBUFFERED=1
- pip install -r requirements.txt
- COPY . /app
- WORKDIR /app
```

### docker/Dockerfile
**Aynı içerikle docker klasörüne kopyalandı**

### .dockerignore
**Yeni dosya:**
- __pycache__, *.pyc, .pytest_cache
- .venv, venv
- .env dosyaları
- logs/, postgres-data/, pgdata/, redis-data/
- .git/, .github/, .cursor/, .idea/, .vscode/

### docker-compose.yml (root)
**Yeni dosya - production deployment için:**

**Servisler:**
1. **postgres**:
   - postgres:15-alpine
   - Auto-load schemas (./db/schemas:/docker-entrypoint-initdb.d:ro)
   - Healthcheck

2. **redis**:
   - redis:7-alpine

3. **collector**:
   - APScheduler scheduler servisi
   - Command: `python -m src.collector.scheduler`
   - Config image içinde (mount yok)

4. **live_loop**:
   - Opsiyonel servis
   - ENABLE_LIVE_LOOP env variable ile kontrol
   - Disabled ise idle kalır (API çağırmaz)
   - Command: conditional sh script

**Özellikler:**
- Tüm servisler aynı network'te (api_football_net)
- postgres ve redis'e depends_on
- Log volume mount (./logs:/app/logs)
- Config mount kaldırıldı (config image içinde)

### docker-compose.live.yml
**Yeni dosya - sadece live_loop servisi:**
- Standalone deployment için
- Ana compose'dan bağımsız çalışabilir

### docker/docker-compose.yml
**Güncellendi:**
- Dockerfile path: docker/Dockerfile
- Config mount kaldırıldı
- LOG_FILE env variable eklendi

**Coolify deploy çözümleri:**
1. Path sorunları giderildi (../Dockerfile → docker/Dockerfile)
2. /artifacts path resolve hatası düzeltildi (root compose ile)
3. Config dosyası bulunamama sorunu giderildi (mount kaldırıldı)
4. live_loop crash sorunu çözüldü (opsiyonel + ENABLE_LIVE_LOOP)

---

## 6. APScheduler Tabanlı Scheduler Servisi

### src/collector/scheduler.py
**Yeni dosya - Production job scheduler:**

**Özellikler:**
- AsyncIOScheduler kullanımı
- Config dosyalarından job yükleme (config/jobs/*.yaml)
- Cron ve Interval trigger desteği
- Job mapping: config → runner fonksiyonları
- EmergencyStopError monitoring
- Graceful shutdown (SIGINT, SIGTERM)

**Desteklenen job tipleri:**
1. **static_bootstrap**: timezones, countries, leagues, teams
2. **incremental_daily**: daily_fixtures_by_date, daily_standings

**Job config shape:**
```yaml
jobs:
  - job_id: "bootstrap_leagues"
    enabled: true
    type: "static_bootstrap"
    endpoint: "/leagues"
    params:
      season: 2024
    interval:
      type: "cron"
      cron: "0 0 * * *"
    filters:
      tracked_leagues: [39, 140, 78]
```

**Komut:** `python -m src.collector.scheduler`

---

## 7. Job Modülleri

### src/jobs/__init__.py
**Yeni dosya - boş package init**

### src/jobs/static_bootstrap.py
**Yeni dosya - Static data job'ları:**

**Fonksiyonlar:**
1. `run_bootstrap_countries()`: /countries endpoint
2. `run_bootstrap_timezones()`: /timezone endpoint
3. `run_bootstrap_leagues(season, tracked_leagues)`: /leagues
4. `run_bootstrap_teams(season, tracked_leagues)`: /teams (her league için)

**Özellikler:**
- APIClient ve RateLimiter parametre olarak alır
- RAW + CORE upsert
- Structured logging

### src/jobs/incremental_daily.py
**Yeni dosya - Daily job'lar:**

**Fonksiyonlar:**
1. `run_daily_fixtures_by_date(target_date_utc, client, limiter, config_path)`:
   - scripts/daily_sync.py'deki sync_daily_fixtures'ı yeniden kullanır

2. `run_daily_standings(client, limiter, config_path)`:
   - utils/standings.py'deki sync_standings'i yeniden kullanır

**Amaç:** Scheduler'dan çağrılabilir, test edilebilir job units

---

## 8. README.md Güncellemesi

**Eklenen bölümler:**

### Docker / Coolify Deploy
```bash
docker compose up -d --build
```

**Required env:**
- API_FOOTBALL_KEY
- DATABASE_URL (veya POSTGRES_*)
- REDIS_URL (live_loop için)

**Live loop enable:**
```bash
ENABLE_LIVE_LOOP=1 docker compose up -d --build
```

**Alternatif:**
```bash
docker compose -f docker-compose.yml -f docker-compose.live.yml up -d --build
```

---

## 9. Production Readiness Değişiklikleri

### Kaldırılan "Yasak" Pattern'ler:

1. **Hard-coded default değerler:**
   - ❌ `--season default=2024`
   - ❌ `TRACKED_LEAGUES_DEFAULT = [39, 140, 78]`
   - ✅ Config dosyasından oku, yoksa fail

2. **Varsayımlar (Assumptions):**
   - ❌ MCP'de `season or 2024`
   - ✅ Season config'de yoksa `season_required` error

3. **Kullanılmayan config:**
   - ❌ rate_limiter.yaml'daki emergency_stop_threshold kullanılmıyordu
   - ✅ RateLimiter'a entegre edildi

### Emergency Stop Flow:
```python
try:
    limiter.acquire_token()
    result = await client.get("/fixtures", params)
    limiter.update_from_headers(result.headers)
except EmergencyStopError as e:
    logger.error("emergency_stop_daily_quota_low", err=str(e))
    break  # Stop scheduling
```

---

## 10. Config Dosyaları Yapısı

### config/api.yaml
```yaml
api:
  base_url: "https://v3.football.api-sports.io"
  api_key_env: "API_FOOTBALL_KEY"
  timeout_seconds: 30.0
  default_timezone: "UTC"
```

### config/rate_limiter.yaml
```yaml
rate_limiter:
  token_bucket_per_minute: 300
  minute_soft_limit: 250
  daily_limit: 100
  emergency_stop_threshold: 10
```

### config/jobs/static.yaml
```yaml
jobs:
  - job_id: "bootstrap_leagues"
    enabled: true
    type: "static_bootstrap"
    params:
      season: 2024
    filters:
      tracked_leagues: [39, 140, 78]
```

### config/jobs/daily.yaml
```yaml
season: 2024
tracked_leagues:
  - id: 39
    name: "Premier League"
  - id: 140
    name: "La Liga"
```

### config/jobs/live.yaml
```yaml
jobs:
  - job_id: "live_fixtures"
    enabled: true
    type: "live_loop"
    endpoint: "/fixtures"
    params:
      live: "all"
    interval:
      type: "interval"
      seconds: 15
    filters:
      tracked_leagues: [39, 140, 78]
```

---

## 11. Deployment Sorunları ve Çözümler

### Sorun 1: Dockerfile not found
**Hata:** `failed to read dockerfile: open Dockerfile: no such file or directory`

**Çözüm:**
- docker/Dockerfile oluşturuldu
- docker/docker-compose.yml içinde `dockerfile: docker/Dockerfile` düzeltildi

### Sorun 2: Path resolve hatası (/artifacts)
**Hata:** `lstat /artifacts/docker: no such file or directory`

**Çözüm:**
- Root dizine docker-compose.yml eklendi
- `context: .` ve `dockerfile: Dockerfile` kullanıldı
- Relative path sorunları ortadan kalktı

### Sorun 3: Config dosyası bulunamadı
**Hata:** `FileNotFoundError: /app/config/api.yaml`

**Çözüm:**
- `./config:/app/config:ro` mount kaldırıldı
- Config dosyaları image içinde COPY ile alındı
- Coolify'da host'ta config/ yoksa mount boş klasör yaratıyordu

### Sorun 4: live_loop crash
**Hata:** `ValueError: Missing tracked leagues for live loop`

**Çözüm:**
- live_loop servisi opsiyonel hale getirildi
- ENABLE_LIVE_LOOP env variable eklendi
- Disabled ise idle kalır (tail -f /dev/null)

---

## 12. Test ve Validasyon

### Mevcut testler:
- `tests/unit/test_rate_limiter.py`
- `tests/unit/test_api_client.py`

### Validation checklist:
- ✅ `/status` endpoint çağrısı
- ✅ Rate limiter token blocking
- ✅ Database schemas (FK kontrolleri)
- ✅ Docker Compose service başlatma
- ✅ Environment variable yükleme

---

## Özet

### Eklenen Dosyalar:
1. src/utils/config.py
2. src/collector/scheduler.py
3. src/jobs/__init__.py
4. src/jobs/static_bootstrap.py
5. src/jobs/incremental_daily.py
6. src/mcp/server.py
7. Dockerfile (root)
8. docker/Dockerfile
9. docker-compose.yml (root)
10. docker-compose.live.yml
11. .dockerignore

### Güncellenen Dosyalar:
1. src/collector/rate_limiter.py (EmergencyStopError, update_from_headers)
2. scripts/daily_sync.py (config-driven)
3. scripts/live_loop.py (config-driven)
4. scripts/bootstrap.py (config-driven)
5. docker/docker-compose.yml
6. README.md

### Kaldırılan Pattern'ler:
- ❌ Hard-coded default değerler
- ❌ Varsayımlar (assumptions)
- ❌ Kullanılmayan config parametreleri

### Production Hazırlığı:
- ✅ Config-driven architecture
- ✅ Emergency stop mekanizması
- ✅ APScheduler tabanlı scheduler
- ✅ Docker/Coolify deployment
- ✅ MCP server (monitoring)
- ✅ Structured logging
- ✅ Graceful shutdown
- ✅ Health checks

### Eksik Kalanlar (Öneriler):
- Circuit breaker mekanizması
- GitHub Actions CI (pytest otomasyonu)
- Retry stratejisi (job failure durumunda)
- Metrics/monitoring endpoint (Prometheus)
- Database migration tool (Alembic)

---

**Son Durum:** Proje production-ready hale getirildi ve Coolify'a deploy edilebilir durumda.
