from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


@lru_cache(maxsize=8)
def daily_tracked_leagues_from_jobs_dir(jobs_dir: str) -> tuple[set[int], int | None]:
    """
    Load tracked league IDs (and optionally a single inferred season) from jobs/daily.yaml.

    Returned:
    - ids: union of daily.yaml tracked_leagues[*].id
    - inferred_season:
      - if daily.yaml has top-level `season`, use it
      - else if ALL tracked_leagues have the same non-null season, use that
      - else None (caller must set season explicitly)
    """
    jobs_path = Path(jobs_dir)
    daily_path = jobs_path / "daily.yaml"
    if not daily_path.exists():
        return set(), None

    try:
        cfg = yaml.safe_load(daily_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return set(), None

    tracked = cfg.get("tracked_leagues") or []
    ids: set[int] = set()
    seasons: set[int] = set()
    if isinstance(tracked, list):
        for x in tracked:
            if not isinstance(x, dict) or "id" not in x:
                continue
            try:
                ids.add(int(x["id"]))
            except Exception:
                continue
            if x.get("season") is not None:
                try:
                    seasons.add(int(x["season"]))
                except Exception:
                    pass

    top_season = cfg.get("season")
    inferred: int | None = None
    if top_season is not None:
        try:
            inferred = int(top_season)
        except Exception:
            inferred = None
    elif len(seasons) == 1:
        inferred = next(iter(seasons))

    return ids, inferred


def apply_bootstrap_scope_inheritance(raw_job: dict[str, Any], *, jobs_dir: Path) -> dict[str, Any]:
    """
    Config-driven defaults for bootstrap jobs:

    - If bootstrap_leagues.filters.tracked_leagues is missing/empty, use daily.yaml tracked_leagues IDs.
    - If bootstrap_teams.mode.tracked_leagues is missing/empty, use daily.yaml tracked_leagues IDs.
    - If params.season is None, try to infer a single season from daily.yaml (safe only when unambiguous).

    This prevents having to maintain the same league list in multiple YAML files.
    """
    job_id = str(raw_job.get("job_id") or "")
    if job_id not in {"bootstrap_leagues", "bootstrap_teams"}:
        return raw_job

    daily_ids, inferred_season = daily_tracked_leagues_from_jobs_dir(str(jobs_dir))

    # Copy-on-write: don't mutate caller dicts
    out = dict(raw_job)
    params = dict(out.get("params") or {})
    filters = dict(out.get("filters") or {})
    mode = dict(out.get("mode") or {})

    if params.get("season") is None and inferred_season is not None:
        params["season"] = int(inferred_season)

    if job_id == "bootstrap_leagues":
        tl = filters.get("tracked_leagues")
        if not isinstance(tl, list) or not tl:
            if daily_ids:
                filters["tracked_leagues"] = sorted(daily_ids)
        out["filters"] = filters
    elif job_id == "bootstrap_teams":
        tl = mode.get("tracked_leagues")
        if not isinstance(tl, list) or not tl:
            if daily_ids:
                mode["tracked_leagues"] = sorted(daily_ids)
        out["mode"] = mode

    out["params"] = params
    return out


