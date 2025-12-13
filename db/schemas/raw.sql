-- RAW LAYER (ARCHIVE)
-- Purpose: store complete API-Football responses for audit/debug/replay.
-- Rules:
-- - Store full response envelope as JSONB
-- - Always store timestamps as UTC (TIMESTAMPTZ)

CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.api_responses (
  id BIGSERIAL PRIMARY KEY,

  endpoint TEXT NOT NULL,
  requested_params JSONB NOT NULL DEFAULT '{}'::jsonb,

  status_code INTEGER NOT NULL,
  response_headers JSONB NOT NULL DEFAULT '{}'::jsonb,

  -- Full API response envelope: {get, parameters, errors, results, paging, response}
  body JSONB NOT NULL,

  -- Convenience fields extracted from envelope (still keep full body above)
  errors JSONB NOT NULL DEFAULT '[]'::jsonb,
  results INTEGER,

  fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index required by Phase 1: (endpoint, fetched_at)
CREATE INDEX IF NOT EXISTS idx_raw_api_responses_endpoint_fetched_at
  ON raw.api_responses (endpoint, fetched_at DESC);


