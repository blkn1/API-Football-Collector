-- Fixture verification state tracking
--
-- Purpose:
-- - Avoid overloading core.fixtures.updated_at as a retry marker
-- - Track verification attempts deterministically
-- - Allow marking fixtures as "not_found" when the upstream API consistently returns empty response
--
-- Columns:
-- - verification_state: pending|verified|not_found|blocked
-- - verification_attempt_count: number of attempts made (only for pending)
-- - verification_last_attempt_at: last attempt timestamp (UTC)

ALTER TABLE core.fixtures
ADD COLUMN IF NOT EXISTS verification_state TEXT;

ALTER TABLE core.fixtures
ADD COLUMN IF NOT EXISTS verification_attempt_count INTEGER;

ALTER TABLE core.fixtures
ADD COLUMN IF NOT EXISTS verification_last_attempt_at TIMESTAMPTZ;

-- Initialize existing flagged fixtures
UPDATE core.fixtures
SET verification_state = COALESCE(verification_state, 'pending'),
    verification_attempt_count = COALESCE(verification_attempt_count, 0)
WHERE needs_score_verification = TRUE;

-- Mark already-verified (non-flagged) fixtures as verified only if they were auto-finished historically.
UPDATE core.fixtures
SET verification_state = COALESCE(verification_state, 'verified')
WHERE needs_score_verification = FALSE
  AND status_long LIKE '%Auto-finished%';

-- Indexes for verification selection and reporting
CREATE INDEX IF NOT EXISTS idx_core_fixtures_verification_state
ON core.fixtures (verification_state);

CREATE INDEX IF NOT EXISTS idx_core_fixtures_verification_last_attempt
ON core.fixtures (verification_last_attempt_at)
WHERE verification_state = 'pending';


