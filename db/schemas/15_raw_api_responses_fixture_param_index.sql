-- RAW performance: accelerate per-fixture endpoint existence checks
-- Used by fixture_details season backfill selectors:
--   WHERE endpoint='/fixtures/*' AND (requested_params->>'fixture')::bigint = <fixture_id>

CREATE INDEX IF NOT EXISTS idx_raw_api_responses_endpoint_fixture
  ON raw.api_responses (endpoint, ((requested_params->>'fixture')::bigint));


