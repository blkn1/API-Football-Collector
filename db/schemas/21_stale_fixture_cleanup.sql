-- One-time migration: Auto-finish existing stale fixtures in database.
--
-- Purpose: Clean up fixtures that are stuck in live/intermediate states (NS, HT, 2H, 1H, LIVE, BT, ET, P, SUSP, INT)
-- even though they should have finished hours ago.
--
-- Safety measures:
-- 1. Double-threshold: date < NOW() - 3 hours AND updated_at < NOW() - 6 hours
-- 2. Only affects tracked leagues (manually verify league_ids in your daily.yaml)
-- 3. RETURNING clause allows you to audit changes before committing
--
-- IMPORTANT: Review output before running in production!
-- Adjust tracked_league_ids array if needed to match your config/jobs/daily.yaml tracked_leagues list.

-- Tracked league IDs (from config/jobs/daily.yaml at time of creation)
-- NOTE: Update this list if tracked_leagues changes in daily.yaml
WITH tracked_leagues AS (
  SELECT ARRAY[
    39, 40, 41, 42, 61, 62, 78, 79, 80, 88, 94, 95, 106, 111, 112, 121,
    135, 136, 140, 141, 144, 145, 162, 179, 180, 183, 184, 185, 188, 197,
    203, 204, 206, 207, 208, 210, 218, 219, 241, 242, 262, 271, 274, 276,
    281, 283, 286, 290, 291, 296, 302, 303, 308, 312, 315, 332, 333, 344,
    345, 370, 380, 381, 390, 393, 396, 399, 407, 408, 419, 492, 516, 570,
    585, 677, 701, 705, 828, 865, 871, 967, 976, 1059, 1168
  ]::BIGINT[] AS league_ids
)
-- Select and auto-finish stale fixtures
UPDATE core.fixtures f
SET
  status_short = 'FT',
  status_long = 'Match Finished (Auto-finished via manual cleanup)',
  updated_at = NOW()
FROM tracked_leagues tl
WHERE
  -- Only tracked leagues
  f.league_id = ANY(tl.league_ids)
  -- Stale intermediate states (not final statuses)
  AND f.status_short IN ('NS', 'HT', '2H', '1H', 'LIVE', 'BT', 'ET', 'P', 'SUSP', 'INT')
  -- Double-threshold safety check
  AND f.date < NOW() - INTERVAL '3 hours'
  AND f.updated_at < NOW() - INTERVAL '6 hours'
RETURNING
  f.id,
  f.league_id,
  f.season,
  f.status_short AS old_status,
  f.status_long AS old_status_long,
  f.date,
  f.updated_at AS old_updated_at;

-- Expected output analysis:
-- 1. Review --> league_ids array to ensure it matches your tracked_leagues
-- 2. Check --> count of returned rows (should be reasonable, not thousands)
-- 3. Verify --> date and updated_at timestamps are indeed old
-- 4. If everything looks correct, --> UPDATE is already committed
-- 5. If you see unexpected rows, you may need to rollback (if in transaction)

-- Rollback SQL (if needed):
-- Note: Since original status values are lost after UPDATE, you can only rollback
-- if you saved --> RETURNING output and manually reconstruct--> UPDATE statement.
-- Best practice: Run this in a transaction first to test:
--   BEGIN;
--   -- Run --> UPDATE above
--   -- Review --> output
--   -- If satisfied: COMMIT;
--   -- If not: ROLLBACK;

-- Verification query (run after migration to confirm):
-- SELECT
--   COUNT(*) AS remaining_stale_fixtures,
--   COUNT(DISTINCT league_id) AS leagues_with_stale_fixtures
-- FROM core.fixtures f
-- JOIN tracked_leagues tl ON 1=1
-- WHERE
--   f.league_id = ANY(tl.league_ids)
--   AND f.status_short IN ('NS', 'HT', '2H', '1H', 'LIVE', 'BT', 'ET', 'P', 'SUSP', 'INT')
--   AND f.date < NOW() - INTERVAL '3 hours'
--   AND f.updated_at < NOW() - INTERVAL '6 hours';
-- Expected: 0 rows remaining
