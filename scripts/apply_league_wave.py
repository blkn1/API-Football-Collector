from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply a rollout wave by shrinking tracked_leagues in configs.")
    parser.add_argument("--size", type=int, default=10, help="Number of leagues to keep (default: 10)")
    parser.add_argument("--offset", type=int, default=0, help="Start offset in tracked_leagues (default: 0)")
    parser.add_argument(
        "--league-ids",
        type=str,
        default=None,
        help="Comma-separated explicit league IDs to keep (overrides --size/--offset)",
    )
    parser.add_argument("--no-backup", action="store_true", help="Do not write .bak backup files")
    args = parser.parse_args()

    daily_path = PROJECT_ROOT / "config" / "jobs" / "daily.yaml"
    live_path = PROJECT_ROOT / "config" / "jobs" / "live.yaml"

    daily = _load_yaml(daily_path)
    tracked = daily.get("tracked_leagues") or []
    if not isinstance(tracked, list) or not tracked:
        raise SystemExit(f"missing tracked_leagues in {daily_path}")

    selected: list[dict[str, Any]] = []
    if args.league_ids:
        wanted = {int(x.strip()) for x in args.league_ids.split(",") if x.strip()}
        for item in tracked:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            if int(item["id"]) in wanted:
                selected.append(item)
        if len(selected) != len(wanted):
            missing = sorted(wanted - {int(x["id"]) for x in selected if isinstance(x, dict) and x.get("id") is not None})
            raise SystemExit(f"league_ids_not_found_in_daily_yaml: {missing}")
    else:
        size = max(0, int(args.size))
        offset = max(0, int(args.offset))
        selected = [x for x in tracked[offset : offset + size] if isinstance(x, dict) and x.get("id") is not None]

    if not selected:
        raise SystemExit("no_leagues_selected")

    if not args.no_backup:
        ts = _utc_ts()
        daily_path.replace(daily_path.with_suffix(daily_path.suffix + f".{ts}.bak"))
        # Re-load after backup move so we preserve exact original content aside from tracked_leagues change
        daily = _load_yaml(daily_path.with_suffix(daily_path.suffix + f".{ts}.bak"))
        tracked = daily.get("tracked_leagues") or []

    # Apply wave
    daily["tracked_leagues"] = selected
    _write_yaml(daily_path, daily)

    # Update live.yaml filters to match
    live = _load_yaml(live_path)
    jobs = live.get("jobs") or []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        if j.get("job_id") == "live_fixtures_all":
            j.setdefault("filters", {})
            j["filters"]["tracked_leagues"] = [int(x["id"]) for x in selected if isinstance(x, dict) and x.get("id") is not None]
    live["jobs"] = jobs
    if not args.no_backup:
        ts = _utc_ts()
        live_path.replace(live_path.with_suffix(live_path.suffix + f".{ts}.bak"))
        live = _load_yaml(live_path.with_suffix(live_path.suffix + f".{ts}.bak"))
        jobs = live.get("jobs") or []
        for j in jobs:
            if not isinstance(j, dict):
                continue
            if j.get("job_id") == "live_fixtures_all":
                j.setdefault("filters", {})
                j["filters"]["tracked_leagues"] = [int(x["id"]) for x in selected if isinstance(x, dict) and x.get("id") is not None]
        live["jobs"] = jobs
    _write_yaml(live_path, live)

    print(f"[OK] wave applied: kept={len(selected)} of total={len(tracked)} leagues")
    print(f"  - updated: {daily_path}")
    print(f"  - updated: {live_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

