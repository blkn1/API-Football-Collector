-- Düzeltilen events'ları kontrol etmek için SQL sorguları
-- Kullanım: psql -U postgres -d api_football -f scripts/check_fixture_events.sql

-- 1. Belirli bir fixture'ın events'larını listele (örnek: 1396373)
\echo '=== Fixture 1396373 Events ==='
SELECT 
    fixture_id,
    time_elapsed,
    time_extra,
    type,
    detail,
    comments,
    team_id,
    player_id,
    updated_at
FROM core.fixture_events
WHERE fixture_id = 1396373
ORDER BY time_elapsed NULLS LAST, time_extra NULLS LAST, updated_at ASC;

-- 2. Fixture'ın detay durumunu kontrol et (hangi endpoint'ler var?)
\echo '=== Fixture 1396373 Detail Status ==='
SELECT
    f.id AS fixture_id,
    f.status_short,
    f.goals_home,
    f.goals_away,
    f.updated_at AS fixture_updated_at,
    EXISTS (SELECT 1 FROM core.fixture_players p WHERE p.fixture_id = f.id) AS has_players,
    EXISTS (SELECT 1 FROM core.fixture_events e WHERE e.fixture_id = f.id) AS has_events,
    EXISTS (SELECT 1 FROM core.fixture_statistics s WHERE s.fixture_id = f.id) AS has_statistics,
    EXISTS (SELECT 1 FROM core.fixture_lineups l WHERE l.fixture_id = f.id) AS has_lineups,
    (SELECT MAX(r.fetched_at) FROM raw.api_responses r WHERE r.endpoint='/fixtures/events' AND (r.requested_params->>'fixture')::bigint=f.id) AS last_events_fetch
FROM core.fixtures f
WHERE f.id = 1396373;

-- 3. Verification job'ın son çalışmasında hangi fixture'lar düzeltildi?
\echo '=== Recently Verified Fixtures (Last 24h) ==='
SELECT 
    f.id,
    f.league_id,
    f.status_short,
    f.goals_home,
    f.goals_away,
    f.needs_score_verification,
    f.updated_at,
    (SELECT COUNT(*) FROM core.fixture_events e WHERE e.fixture_id = f.id) AS events_count,
    (SELECT MAX(r.fetched_at) FROM raw.api_responses r WHERE r.endpoint='/fixtures/events' AND (r.requested_params->>'fixture')::bigint=f.id) AS last_events_fetch
FROM core.fixtures f
WHERE f.status_short = 'FT'
  AND f.updated_at >= NOW() - INTERVAL '24 hours'
  AND EXISTS (SELECT 1 FROM core.fixture_events e WHERE e.fixture_id = f.id)
ORDER BY f.updated_at DESC
LIMIT 20;

-- 4. Verification job'ın RAW log'larını kontrol et
\echo '=== Verification Job RAW Logs (Last 2h) ==='
SELECT 
    endpoint,
    requested_params,
    status_code,
    results,
    fetched_at
FROM raw.api_responses
WHERE endpoint = '/fixtures/events'
  AND fetched_at >= NOW() - INTERVAL '2 hours'
ORDER BY fetched_at DESC
LIMIT 10;

-- 5. Auto-finished ve verification edilmiş fixture'lar
\echo '=== Auto-finished + Verified Fixtures ==='
SELECT 
    f.id,
    f.league_id,
    f.status_long,
    f.goals_home,
    f.goals_away,
    f.needs_score_verification,
    f.updated_at,
    (SELECT COUNT(*) FROM core.fixture_events e WHERE e.fixture_id = f.id) AS events_count
FROM core.fixtures f
WHERE f.status_long LIKE '%Auto-finished%'
  AND f.needs_score_verification = FALSE
  AND EXISTS (SELECT 1 FROM core.fixture_events e WHERE e.fixture_id = f.id)
ORDER BY f.updated_at DESC
LIMIT 20;

