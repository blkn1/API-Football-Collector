# API-Football Data Collector

Production-grade, **config-driven**, **quota-safe** data pipeline for **API-Football v3** (RAW → CORE → MART) + MCP (read-only monitoring).

High-signal docs:
- `production-v3.md` (Production v3.0 architecture + Coolify deploy)
- `QUOTA_AND_COLLECTION.md` (quota math + job cadence + throughput knobs)
- `env_info.md` (Coolify ENV reference: what/where/impact)
- `LEAGUE_TRACKING_GUIDE.md` (lig ekleme, live vs daily, takım farklı ligde oynarsa ne olur?)

## What This Repo Contains (Production v3.0)

- **Config layer**: `config/` (API, rate limiter, jobs, coverage) — hard-code yok.
- **Collector**: `src/collector/` + `src/jobs/` (APScheduler + rate limiter + retry/backoff patterns)
- **Data layers (PostgreSQL)**: `db/schemas/` (RAW JSONB archive → CORE normalized UPSERT → MART coverage)
- **Backfill**: `src/jobs/backfill.py` (resumeable `core.backfill_progress`)
- **MCP**: `src/mcp/` (read-only tools: coverage, db stats, fixtures/standings/injuries/fixture_details queries)
- **Docker / Coolify**: root `docker-compose.yml` (collector + live_loop + mcp + read_api)
- **Healthchecks**: `scripts/healthcheck_*.py`
- **Tests**: `tests/unit/`, `tests/integration/`, `tests/mcp/`

Operational safety nets (prod):
- **Per-league daily fixtures** (`fixtures_fetch_mode: per_tracked_leagues`): ingests fixtures only for `config/jobs/daily.yaml -> tracked_leagues`.
- **Resumeable fixtures backfill** (`fixtures_backfill_league_season`): fills current season history over time using `core.backfill_progress` (quota-safe).

## Setup (local)

### 0) Prerequisites

- **Docker + Compose**: this project expects `docker` and `docker compose` to be available.
  - If you see `docker: command not found`, install Docker Engine + the Compose plugin on Ubuntu: [`https://docs.docker.com/engine/install/ubuntu/`]

### 1) Environment

- Ensure `.env` exists at repo root and contains:
  - `API_FOOTBALL_KEY=...`
  - (optional) `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`

### 2) Install Dependencies

```bash
python3 -m pip install -r requirements.txt
```

### 3) Start services (local)

```bash
docker compose up -d --build
```

On startup, the collector runs `python scripts/apply_schemas.py` which applies `db/schemas/*.sql` idempotently (safe for persistent volumes).

## Docker / Coolify Deploy

This repo ships a root `Dockerfile` and a Compose stack in `docker-compose.yml` (repo root).

- **collector**: APScheduler service that runs enabled non-live jobs from `config/jobs/*.yaml`
- **live_loop**: optional `/fixtures?live=all` poller (15s). Controlled by `ENABLE_LIVE_LOOP=1`. Default: off.
- **mcp**: read-only query interface (Coolify: HTTP/SSE)
- **read_api**: dış tüketim için read-only REST + SSE (ops, n8n, dashboard)

Minimal steps:

```bash
docker compose up -d --build
```

Required environment:
- `API_FOOTBALL_KEY`
- `DATABASE_URL` (recommended) or `POSTGRES_*`
- `SCHEDULER_TIMEZONE` (recommended, e.g. `Europe/Istanbul` for TR cron evaluation)
- `REDIS_URL` (only needed if `ENABLE_LIVE_LOOP=1`)

MCP (Coolify / HTTP-SSE):
- `MCP_TRANSPORT=streamable-http` (prod default)
- `FASTMCP_HOST=0.0.0.0`
- `FASTMCP_PORT=8000`
- `MCP_MOUNT_PATH=/mcp`

Notlar:
- Streamable HTTP MCP **stateful** çalışır (session + initialize). Ayrıntılar: `MCP_USAGE_GUIDE.md`.
- Claude Desktop prod MCP’ye bağlanırken stdio→streamable-http adapter kullanır. Ayrıntılar: `MCP_USAGE_GUIDE.md` (bölüm 4).
- Prod smoke test: `bash scripts/smoke_mcp.sh`

Read API (REST + SSE):
- Health: `GET /v1/health`
- Quota: `GET /v1/quota`
- Fixtures: `GET /v1/fixtures?date=YYYY-MM-DD&limit=200`
- Live scores (SSE): `GET /v1/sse/live-scores`
- Prod smoke test (curl): `bash scripts/smoke_read_api.sh`

Prod domain (örnek):
- `https://readapi.zinalyze.pro`

Enable live loop (optional):

```bash
ENABLE_LIVE_LOOP=1 docker compose up -d --build
```

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

If you’re running in Coolify and want a safe smoke-test locally first:
- `python scripts/test_api.py` (FREE `/status`)
- `pytest -q tests/unit/test_rate_limiter.py`



