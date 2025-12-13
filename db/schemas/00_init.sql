-- Ensures deterministic init order when mounted into /docker-entrypoint-initdb.d
-- Docker runs init scripts alphabetically; default order is:
--   core.sql -> mart.sql -> raw.sql
-- but mart.sql depends on raw.api_responses.
--
-- Fix: run raw.sql first; core.sql will still run before mart.sql afterwards.
\i /docker-entrypoint-initdb.d/raw.sql


