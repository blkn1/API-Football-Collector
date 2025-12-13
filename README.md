# API-Football Data Collector

Production-grade, config-driven data pipeline for **API-Football v3**.

## What This Repo Contains (Phase 1: Foundation)

- **Config layer**: `config/` (API, rate limiter, jobs, coverage)
- **Data layers (PostgreSQL)**: `db/schemas/` (RAW → CORE → MART)
- **Collector foundation**:
  - `src/collector/api_client.py` (GET-only, `x-apisports-key` only, async httpx, status-code handling)
  - `src/collector/rate_limiter.py` (in-memory token bucket, thread-safe)
- **Docker**: `docker/docker-compose.yml` (postgres + redis, schema auto-load)
- **Test script**: `scripts/test_api.py` (calls `/status` only)
- **Tests**: `tests/unit/`

## Setup

### 1) Environment

- Ensure `.env` exists at repo root and contains:
  - `API_FOOTBALL_KEY=...`
  - (optional) `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`

### 2) Install Dependencies

```bash
python3 -m pip install -r requirements.txt
```

### 3) Start Postgres + Redis (local)

```bash
cd docker
docker compose up -d
```

Postgres will auto-load SQL schemas from `db/schemas/` on first init.

## Phase 1 Validation Checklist

- [ ] **Can call `/status` successfully** (FREE endpoint)
  - `python3 scripts/test_api.py`
- [ ] **Rate limiter blocks when tokens exhausted**
  - `pytest -q tests/unit/test_rate_limiter.py`
- [ ] **Database schemas created without FK errors**
  - `docker compose up` (first init loads `raw.sql`, `core.sql`, `mart.sql`)
- [ ] **Docker Compose brings up services**
  - `docker compose ps`
- [ ] **Environment variables load correctly**
  - `pytest -q tests/unit/test_api_client.py::test_status_endpoint`

## Run Tests

```bash
pytest -q
```


