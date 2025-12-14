## Decision: What we enable (quota-safe)

- **Daily quota**: Your `/status` shows `limit_day = 7500` on Pro. Your earlier output `daily_remaining=2704` is consistent with that (7500 - 4796 used).
- **Minute quota**: ~300/min hard ceiling; we run with a **soft limit** from `config/rate_limiter.yaml` (`minute_soft_limit: 250`).

### Enabled now (recommended)

- **collector scheduler**: enabled (already running)
- **daily fixtures**: enabled (one run per day, local time via `SCHEDULER_TIMEZONE`)
- **daily standings**: enabled (one run per day, local time)
- **live loop**: **disabled** (too expensive for 7,500/day unless time-windowed)

### Why live loop stays off

`/fixtures?live=all` every 15s ≈ 4 req/min ≈ 5,760 req/day by itself. This can consume most of 7,500/day and starve daily jobs.

### Critical safety switch (already implemented)

- **Emergency stop**: if daily remaining < `emergency_stop_threshold` (default 1000), the collector stops to prevent quota exhaustion.

### IMPORTANT: Venues backfill is OFF by default

`/venues?id=...` is per-venue (many calls). We gate it behind:

- `VENUES_BACKFILL_MAX_PER_RUN` (default `0`)

Set it to a small number (e.g. 5) only if you really need venue enrichment.

---

## Interactive Documentation Panel (Read-only) – Roadmap

### Goal

Build a **read-only web page** that:
- shows current system status
- explains how to change configuration step-by-step
- provides copy/paste commands
- **never** changes anything automatically

### Scope (Phase 1)

#### Pages

- **Dashboard**
  - quota status (daily/minute remaining)
  - last job runs (success/error)
  - DB counts (RAW/CORE/MART)
  - coverage summary by league

- **How to Configure**
  - “Add a league” steps
  - “Enable/disable daily jobs” steps
  - “Enable live loop safely” steps (time window + budget)

- **Runbooks**
  - “Schemas missing” fix
  - “DB does not exist” fix
  - “Rate limit exceeded” fix

#### Data sources (read-only)

- **MCP tools** (preferred)
  - `get_rate_limit_status()`
  - `get_job_status()`
  - `get_database_stats()`
  - `get_coverage_summary()` / `get_coverage_status()`

or (fallback)
- direct read-only SQL queries against Postgres (SELECT-only)

#### Deployment

- Host as a small container (e.g. Next.js or static + small API).
- Auth: basic auth or IP allowlist (since it exposes operational data).

### Phase 2+ (later)

- “League selector” view
- live fixtures read model (MART view)
- exportable “ops report” (markdown/pdf)


