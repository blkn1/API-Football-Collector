-- Add verification flag for auto-finished fixtures
--
-- Purpose: Track fixtures that were auto-finished without fresh API data.
-- These fixtures can be refreshed later when quota allows to verify/correct scores.
--
-- Migration:
-- 1. Add needs_score_verification column (default FALSE)
-- 2. Create index for efficient querying
-- 3. Set flag to TRUE for existing auto-finished matches

-- Add column
ALTER TABLE core.fixtures
ADD COLUMN IF NOT EXISTS needs_score_verification BOOLEAN NOT NULL DEFAULT FALSE;

-- Create index for efficient querying
CREATE INDEX IF NOT EXISTS idx_core_fixtures_needs_verification
ON core.fixtures (needs_score_verification)
WHERE needs_score_verification = TRUE;

-- Set flag for existing auto-finished matches
UPDATE core.fixtures
SET needs_score_verification = TRUE
WHERE status_long LIKE '%Auto-finished%'
  AND needs_score_verification = FALSE;

