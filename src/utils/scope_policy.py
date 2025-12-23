from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from src.utils.db import get_db_connection, query_scalar
from src.utils.logging import get_logger

logger = get_logger(component="scope_policy")


@dataclass(frozen=True)
class ScopeDecision:
    in_scope: bool
    reason: str
    policy_version: int
    league_type: str | None = None


@dataclass(frozen=True)
class ScopePolicy:
    version: int
    baseline_enabled_endpoints: set[str]
    by_competition_type: dict[str, dict[str, set[str]]]
    overrides: list[dict[str, Any]]


def _default_policy_path() -> Path:
    # repo root = .../src/utils/.. (2 levels up from src/)
    return Path(__file__).resolve().parents[2] / "config" / "scope_policy.yaml"


def load_scope_policy(path: Path | None = None) -> ScopePolicy:
    p = path or _default_policy_path()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    version = int(raw.get("version") or 1)
    baseline = set(map(str, raw.get("baseline_enabled_endpoints") or []))

    by_type_raw = raw.get("by_competition_type") or {}
    by_type: dict[str, dict[str, set[str]]] = {}
    if isinstance(by_type_raw, dict):
        for t, cfg in by_type_raw.items():
            if not isinstance(cfg, dict):
                continue
            enabled = set(map(str, cfg.get("enabled_endpoints") or []))
            disabled = set(map(str, cfg.get("disabled_endpoints") or []))
            by_type[str(t)] = {"enabled_endpoints": enabled, "disabled_endpoints": disabled}

    overrides = raw.get("overrides") or []
    if not isinstance(overrides, list):
        overrides = []

    return ScopePolicy(
        version=version,
        baseline_enabled_endpoints=baseline,
        by_competition_type=by_type,
        overrides=overrides,
    )


def _league_type_from_core(league_id: int) -> str | None:
    """
    Return core.leagues.type (API-Football 'League' or 'Cup') when present.
    If not present, return None.
    """
    try:
        v = query_scalar("SELECT type FROM core.leagues WHERE id=%s", (int(league_id),))
        if v is None:
            return None
        return str(v)
    except Exception:
        return None


def get_league_types_map(league_ids: list[int]) -> dict[int, str]:
    """
    Bulk fetch league types from core.leagues for the given IDs.
    Missing IDs are omitted from the result.
    """
    ids = [int(x) for x in league_ids if x is not None]
    if not ids:
        return {}
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, type FROM core.leagues WHERE id = ANY(%s)", (ids,))
                rows = cur.fetchall()
            conn.commit()
        out: dict[int, str] = {}
        for r in rows:
            try:
                out[int(r[0])] = str(r[1])
            except Exception:
                continue
        return out
    except Exception as e:
        logger.warning("scope_policy_bulk_league_type_failed", err=str(e))
        return {}


def _apply_overrides(
    *,
    policy: ScopePolicy,
    league_id: int,
    season: int,
    endpoint: str,
) -> tuple[bool | None, str | None]:
    """
    Return (forced_in_scope, reason) if an override applies, otherwise (None, None).
    """
    for o in policy.overrides:
        if not isinstance(o, dict):
            continue
        try:
            if int(o.get("league_id")) != int(league_id):
                continue
        except Exception:
            continue

        s = o.get("season")
        if s is not None:
            try:
                if int(s) != int(season):
                    continue
            except Exception:
                continue

        disabled = set(map(str, o.get("disabled_endpoints") or []))
        enabled = set(map(str, o.get("enabled_endpoints") or []))

        if endpoint in disabled:
            return False, "override_disabled"
        if endpoint in enabled:
            return True, "override_enabled"

    return None, None


def decide_scope(
    *,
    league_id: int,
    season: int,
    endpoint: str,
    policy: ScopePolicy | None = None,
    league_type_provider: Callable[[int], str | None] | None = None,
) -> ScopeDecision:
    """
    Decide whether `endpoint` is in-scope for a given (league_id, season).

    Safety rule: when league type is unknown, FAIL OPEN (in_scope=True) to avoid
    dropping valuable data due to missing metadata.
    """
    pol = policy or load_scope_policy()
    ep = str(endpoint)

    # Baseline endpoints are always enabled.
    if ep in pol.baseline_enabled_endpoints:
        return ScopeDecision(in_scope=True, reason="baseline_enabled", policy_version=pol.version)

    # Overrides (explicit allow/deny) win.
    forced, reason = _apply_overrides(policy=pol, league_id=int(league_id), season=int(season), endpoint=ep)
    if forced is not None:
        return ScopeDecision(in_scope=bool(forced), reason=str(reason), policy_version=pol.version)

    # Type-based defaults.
    lt_provider = league_type_provider or _league_type_from_core
    league_type = lt_provider(int(league_id))
    if league_type is None:
        return ScopeDecision(
            in_scope=True,
            reason="league_type_unknown_fail_open",
            policy_version=pol.version,
            league_type=None,
        )

    type_cfg = pol.by_competition_type.get(str(league_type)) or {}
    enabled = type_cfg.get("enabled_endpoints") or set()
    disabled = type_cfg.get("disabled_endpoints") or set()

    if ep in disabled:
        return ScopeDecision(
            in_scope=False,
            reason=f"type_{league_type}_disabled",
            policy_version=pol.version,
            league_type=str(league_type),
        )

    if enabled:
        # If allowlist exists for this type, only those endpoints are enabled (besides baseline).
        if ep in enabled:
            return ScopeDecision(
                in_scope=True,
                reason=f"type_{league_type}_enabled",
                policy_version=pol.version,
                league_type=str(league_type),
            )
        return ScopeDecision(
            in_scope=False,
            reason=f"type_{league_type}_not_in_enabled_list",
            policy_version=pol.version,
            league_type=str(league_type),
        )

    # If neither enabled nor disabled lists are defined for this type, default allow.
    return ScopeDecision(in_scope=True, reason=f"type_{league_type}_default_allow", policy_version=pol.version, league_type=str(league_type))


def filter_tracked_leagues_for_endpoint(
    *,
    leagues: list[dict[str, Any]],
    endpoint: str,
    policy: ScopePolicy | None = None,
    league_type_provider: Callable[[int], str | None] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Split tracked league dicts into (in_scope, out_of_scope) for a given endpoint.

    Each league dict must include:
    - id
    - season
    """
    pol = policy or load_scope_policy()
    in_scope: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []

    for l in leagues:
        try:
            league_id = int(l["id"])
            season = int(l["season"])
        except Exception:
            # If malformed, keep in_scope to avoid accidental drops; caller may validate separately.
            in_scope.append(l)
            continue

        d = decide_scope(
            league_id=league_id,
            season=season,
            endpoint=endpoint,
            policy=pol,
            league_type_provider=league_type_provider,
        )
        if d.in_scope:
            in_scope.append(l)
        else:
            out.append({**l, "scope_reason": d.reason, "policy_version": d.policy_version, "league_type": d.league_type})

    return in_scope, out


