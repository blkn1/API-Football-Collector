from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from collector.api_client import APIClient  # noqa: E402
from collector.rate_limiter import RateLimiter  # noqa: E402
from utils.config import load_rate_limiter_config  # noqa: E402


_WOMEN_AMATEUR_PAT = re.compile(r"(women|kadın|kadin|femenina|amatör|amator|amateur)", re.IGNORECASE)

# Aliases for tricky TR names -> expected API league names (best-effort).
LEAGUE_NAME_ALIASES = {
    "FIFA Kıtalararası Kupa": "FIFA Intercontinental Cup",
}

# Minimal TR->EN country mapping for API-Football 'country' field (can be extended).
COUNTRY_TR_TO_API = {
    "Türkiye": "Turkey",
    "İngiltere": "England",
    "İtalya": "Italy",
    "İspanya": "Spain",
    "Almanya": "Germany",
    "Fransa": "France",
    "Portekiz": "Portugal",
    "Hollanda": "Netherlands",
    "Yunanistan": "Greece",
    "Belçika": "Belgium",
    "Avusturya": "Austria",
    "Çekya": "Czech Republic",
    "İsviçre": "Switzerland",
    "İskoçya": "Scotland",
    "Polonya": "Poland",
    "Cezayir": "Algeria",
    "Meksika": "Mexico",
    "Peru": "Peru",
    "Hırvatistan": "Croatia",
    "Nijerya": "Nigeria",
    "Avustralya": "Australia",
    "Macaristan": "Hungary",
    "Endonezya": "Indonesia",
    "Azerbaycan": "Azerbaijan",
    "Brezilya": "Brazil",
    "Danimarka": "Denmark",
    "Hong Kong": "Hong-Kong",
    "Nikaragua": "Nicaragua",
    "Kosta Rika": "Costa-Rica",
    "El Salvador": "El-Salvador",
    "Kuzey İrlanda": "Northern-Ireland",
    "Romanya": "Romania",
    "Slovakya": "Slovakia",
    "Ukrayna": "Ukraine",
    "Birleşik Arap Emirlikleri": "United-Arab-Emirates",
    "Galler": "Wales",
    "Tunus": "Tunisia",
    "Gana": "Ghana",
    "Malta": "Malta",
    "Ekvador": "Ecuador",
    "Kolombiya": "Colombia",
    "Katar": "Qatar",
    "Suudi Arabistan": "Saudi-Arabia",
    "Sırbistan": "Serbia",
    "İran": "Iran",
    "Bosna Hersek": "Bosnia",
    "Bolivya": "Bolivia",
    "Andorra": "Andorra",
    "Kenya": "Kenya",
    "Tayland": "Thailand",
    "Uganda": "Uganda",
    "Lebanon": "Lebanon",
}


def _norm(s: str) -> str:
    s = s.strip().lower()
    # Turkish chars -> ascii-ish (simple)
    s = (
        s.replace("ı", "i")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ş", "s")
        .replace("ö", "o")
        .replace("ç", "c")
        .replace("’", "'")
    )
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())

def _expand_queries(q: str) -> list[str]:
    """
    Generate alternative query strings to improve matching between TR labels and API names.
    """
    base = q.strip()
    # Drop parentheses notes
    q2 = re.sub(r"\([^)]*\)", "", base).strip()
    alts = {base, q2}

    # Common TR -> EN keyword swaps (best-effort)
    for x in list(alts):
        # lig -> league
        alts.add(re.sub(r"\blig\b", "league", x, flags=re.IGNORECASE))
        # kupa/kupası -> cup
        alts.add(re.sub(r"\bkupasi\b|\bkupası\b|\bkupa\b", "cup", x, flags=re.IGNORECASE))
        # süper -> super
        alts.add(re.sub(r"\bsuper\b|\bsuper\b|\bsuper\b", "super", x, flags=re.IGNORECASE))

    # Specific league naming patterns (country-specific but helps scoring)
    alts.add(base.replace("Premier Lig", "Premier League"))
    alts.add(base.replace("Süper Lig", "Super League"))
    alts.add(base.replace("Pro Lig", "Pro League"))
    alts.add(base.replace("1. Lig", "League One"))
    alts.add(base.replace("2. Lig", "League Two"))
    alts.add(base.replace("Premier Lig", "Primeira Liga"))  # Portugal common naming
    alts.add(base.replace("La Liga 2", "LaLiga2"))
    alts.add(base.replace("La Liga 2", "Segunda Division"))

    # Normalize unicode and cleanup
    out: list[str] = []
    for a in alts:
        a = a.strip()
        if a:
            out.append(a)
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class Target:
    raw_line: str
    country_tr: str | None
    country_api: str | None
    league_query: str


def _parse_targets(path: Path) -> list[Target]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]

    # Remove women/amateur lines
    lines = [ln for ln in lines if not _WOMEN_AMATEUR_PAT.search(ln)]

    # Longest-prefix country match
    country_keys = sorted(COUNTRY_TR_TO_API.keys(), key=lambda x: len(x), reverse=True)
    out: list[Target] = []
    for ln in lines:
        # Apply alias on the whole line (before parsing).
        ln = LEAGUE_NAME_ALIASES.get(ln, ln)
        matched: str | None = None
        for ck in country_keys:
            if ln.startswith(ck + " "):
                matched = ck
                break
            if ln == ck:
                matched = ck
                break
        if matched:
            league_query = ln[len(matched) :].strip()
            if not league_query:
                raise SystemExit(f"missing_league_name:{ln}")
            out.append(Target(raw_line=ln, country_tr=matched, country_api=COUNTRY_TR_TO_API[matched], league_query=league_query))
            continue

        # No country prefix: treat as a global competition; search across all countries.
        out.append(Target(raw_line=ln, country_tr=None, country_api=None, league_query=ln))
    return out


def _score(candidate_name: str, query: str) -> int:
    """
    Very simple scoring:
    - exact normalized equality -> 100
    - normalized contains -> 80
    - token overlap ratio -> 0..70
    """
    cn = _norm(candidate_name)
    qn = _norm(query)
    if cn == qn:
        return 100
    if qn and (qn in cn or cn in qn):
        return 80
    c_tokens = set(cn.split())
    q_tokens = set(qn.split())
    if not c_tokens or not q_tokens:
        return 0
    overlap = len(c_tokens.intersection(q_tokens))
    return int(70 * (overlap / max(1, len(q_tokens))))


def _current_season_year(seasons: list[dict[str, Any]]) -> int | None:
    years: list[int] = []
    for s in seasons or []:
        try:
            y = int(s.get("year"))
            years.append(y)
            if s.get("current") is True:
                return y
        except Exception:
            continue
    return max(years) if years else None


async def _fetch_leagues_catalog(*, client: APIClient, limiter: RateLimiter) -> dict[str, Any]:
    return await _safe_get(client=client, limiter=limiter, endpoint="/leagues", params={"current": "true"}, label="/leagues")

async def _fetch_leagues_by_country(*, client: APIClient, limiter: RateLimiter, country_api: str) -> dict[str, Any]:
    return await _safe_get(
        client=client,
        limiter=limiter,
        endpoint="/leagues",
        params={"current": "true", "country": country_api},
        label=f"/leagues(country={country_api})",
    )

async def _fetch_leagues_by_country_all(*, client: APIClient, limiter: RateLimiter, country_api: str) -> dict[str, Any]:
    """
    Full catalog for a country (no 'current' filter).
    This is used only as a fallback for leagues/cups that aren't flagged as current.
    """
    return await _safe_get(
        client=client,
        limiter=limiter,
        endpoint="/leagues",
        params={"country": country_api},
        label=f"/leagues(country_all={country_api})",
    )

async def _fetch_leagues_search(*, client: APIClient, limiter: RateLimiter, search: str) -> dict[str, Any]:
    """
    IMPORTANT: API-Football does not allow combining 'search' with 'country' or 'current'.
    So we fetch with search only, then filter client-side.
    """
    params: dict[str, Any] = {"search": search}
    return await _safe_get(
        client=client,
        limiter=limiter,
        endpoint="/leagues",
        params=params,
        label=f"/leagues(search={search})",
    )

async def _safe_get(
    *,
    client: APIClient,
    limiter: RateLimiter,
    endpoint: str,
    params: dict[str, Any],
    label: str,
    max_retries: int = 6,
) -> dict[str, Any]:
    """
    Production-safe GET wrapper:
    - uses local token bucket to avoid bursting
    - updates quota from headers
    - if API returns rateLimit error in envelope, backs off and retries
    """
    backoff = 2.0
    for attempt in range(max_retries):
        limiter.acquire_token()
        res = await client.get(endpoint, params=params)
        limiter.update_from_headers(res.headers)
        env = res.data or {}
        errors = env.get("errors") or {}
        # API-Football may return 200 with errors.rateLimit
        if isinstance(errors, dict) and errors.get("rateLimit"):
            if attempt == max_retries - 1:
                raise SystemExit(f"api_errors:{label}:{errors}")
            time.sleep(min(backoff, 30.0))
            backoff = min(backoff * 2.0, 30.0)
            continue
        if errors:
            raise SystemExit(f"api_errors:{label}:{errors}")
        return env
    raise SystemExit(f"api_errors:{label}:max_retries_exceeded")


def _extract_candidates(env: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in env.get("response") or []:
        league = item.get("league") or {}
        country = item.get("country") or {}
        seasons = item.get("seasons") or []
        try:
            lid = int(league.get("id"))
        except Exception:
            continue
        out.append(
            {
                "id": lid,
                "name": str(league.get("name") or ""),
                "type": str(league.get("type") or ""),
                "country": str(country.get("name") or ""),
                "season": _current_season_year(seasons),
            }
        )
    return out


def _resolve(targets: list[Target], candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []

    by_country: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_country.setdefault(_norm(c["country"]), []).append(c)

    for t in targets:
        if t.country_api is None:
            cands = candidates
        else:
            cands = by_country.get(_norm(t.country_api), [])
        if not cands:
            unresolved.append(f"{t.raw_line} -> no_candidates_for_country({t.country_api})")
            continue

        best = None
        best_score = -1
        for c in cands:
            # Try multiple query variants
            sc = 0
            for q in _expand_queries(t.league_query):
                sc = max(sc, _score(c["name"], q))
            if sc > best_score:
                best_score = sc
                best = c

        if not best or best_score < 55:
            unresolved.append(f"{t.raw_line} -> low_confidence(best_score={best_score})")
            continue

        if not best.get("season"):
            unresolved.append(f"{t.raw_line} -> missing_current_season_for_league_id({best.get('id')})")
            continue

        resolved.append(
            {
                "id": int(best["id"]),
                "name": best["name"],
                "country": best["country"],
                "type": best["type"],
                "season": int(best["season"]),
                "source": t.raw_line,
                "match_score": int(best_score),
            }
        )
    return resolved, unresolved


def _write_yaml(path: Path, data: Any) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _apply_to_configs(*, resolved: list[dict[str, Any]]) -> None:
    daily_path = PROJECT_ROOT / "config" / "jobs" / "daily.yaml"
    live_path = PROJECT_ROOT / "config" / "jobs" / "live.yaml"

    daily = yaml.safe_load(daily_path.read_text(encoding="utf-8")) or {}
    live = yaml.safe_load(live_path.read_text(encoding="utf-8")) or {}

    # Update tracked leagues list used by scripts + scheduler.
    daily["tracked_leagues"] = [{"id": r["id"], "name": r["name"], "season": r["season"]} for r in resolved]
    # Remove top-level season assumption; per-league season is authoritative.
    if "season" in daily:
        daily.pop("season", None)

    # Enable daily jobs with local-time cron defaults (scheduler timezone controls interpretation).
    jobs = daily.get("jobs") or []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        if j.get("job_id") == "daily_fixtures_by_date":
            j["enabled"] = True
            j.setdefault("interval", {})
            j["interval"]["type"] = "cron"
            j["interval"]["cron"] = "15 6 * * *"  # 06:15 local
        if j.get("job_id") == "daily_standings":
            j["enabled"] = True
            j.setdefault("interval", {})
            j["interval"]["type"] = "cron"
            j["interval"]["cron"] = "45 6 * * *"  # 06:45 local
    daily["jobs"] = jobs

    # Live loop tracked leagues ids only.
    live_jobs = live.get("jobs") or []
    for j in live_jobs:
        if not isinstance(j, dict):
            continue
        if j.get("job_id") == "live_fixtures_all":
            j.setdefault("filters", {})
            j["filters"]["tracked_leagues"] = [r["id"] for r in resolved]
    live["jobs"] = live_jobs

    daily_path.write_text(yaml.safe_dump(daily, sort_keys=False, allow_unicode=True), encoding="utf-8")
    live_path.write_text(yaml.safe_dump(live, sort_keys=False, allow_unicode=True), encoding="utf-8")


async def amain() -> int:
    parser = argparse.ArgumentParser(description="Resolve tracked leagues to API-Football league IDs + current seasons")
    parser.add_argument("--targets", type=str, default=str(PROJECT_ROOT / "config" / "league_targets.txt"))
    parser.add_argument("--out", type=str, default=str(PROJECT_ROOT / "config" / "resolved_tracked_leagues.yaml"))
    parser.add_argument("--apply", action="store_true", help="Update config/jobs/daily.yaml + config/jobs/live.yaml with resolved IDs")
    args = parser.parse_args()

    targets = _parse_targets(Path(args.targets))

    client = APIClient()
    # IMPORTANT: RateLimiter.refill_rate is tokens/second. Use config soft limit per minute to avoid bursting.
    rl_cfg = load_rate_limiter_config()
    max_tokens = int(rl_cfg.minute_soft_limit)
    limiter = RateLimiter(max_tokens=max_tokens, refill_rate=float(max_tokens) / 60.0, emergency_stop_threshold=rl_cfg.emergency_stop_threshold)
    try:
        # Phase 1: global current leagues catalog
        env = await _fetch_leagues_catalog(client=client, limiter=limiter)
        candidates = _extract_candidates(env)
        resolved1, unresolved1 = _resolve(targets, candidates)

        # Phase 2: for unresolved, fetch per-country current catalogs (reduces ambiguity)
        unresolved_targets: list[Target] = []
        for u in unresolved1:
            # u is like "<raw_line> -> ..."
            raw_line = u.split("->", 1)[0].strip()
            for t in targets:
                if t.raw_line == raw_line:
                    unresolved_targets.append(t)
                    break

        extra_candidates: list[dict[str, Any]] = []
        country_cache: dict[str, list[dict[str, Any]]] = {}
        for t in unresolved_targets:
            if not t.country_api:
                continue
            if t.country_api in country_cache:
                continue
            env_c = await _fetch_leagues_by_country(client=client, limiter=limiter, country_api=t.country_api)
            country_cache[t.country_api] = _extract_candidates(env_c)
        for _c, lst in country_cache.items():
            extra_candidates.extend(lst)

        resolved2, unresolved2 = _resolve(unresolved_targets, candidates + extra_candidates)

        resolved = resolved1 + [r for r in resolved2 if r["id"] not in {x["id"] for x in resolved1}]
        unresolved = unresolved2

        # Phase 3: fallback - per-target /leagues?search=... (kept small; only for remaining unresolved)
        if unresolved:
            final_resolved: list[dict[str, Any]] = []
            final_unresolved: list[str] = []
            for u in unresolved:
                raw_line = u.split("->", 1)[0].strip()
                t = next((x for x in unresolved_targets if x.raw_line == raw_line), None)
                if not t:
                    final_unresolved.append(u)
                    continue
                # pick a short search token (best-effort)
                search = re.sub(r"\([^)]*\)", "", t.league_query).strip()
                if len(search) > 40:
                    search = " ".join(search.split()[:4])
                env_s = await _fetch_leagues_search(client=client, limiter=limiter, search=search)
                cand_s_all = _extract_candidates(env_s)
                # Client-side filter: if country specified, keep only that country.
                if t.country_api:
                    cand_s = [c for c in cand_s_all if _norm(c.get("country") or "") == _norm(t.country_api)]
                else:
                    cand_s = cand_s_all
                r3, u3 = _resolve([t], cand_s)
                if r3:
                    final_resolved.extend(r3)
                else:
                    final_unresolved.append(f"{t.raw_line} -> unresolved_after_search(search={search})")

            if final_resolved:
                resolved = resolved + [r for r in final_resolved if r["id"] not in {x["id"] for x in resolved}]
            unresolved = final_unresolved

        # Phase 4: strongest fallback - per-country full catalog (no current filter).
        # This is more expensive in payload size but still low request count because we cache per country.
        if unresolved:
            remaining_targets: list[Target] = []
            for u in unresolved:
                raw_line = u.split("->", 1)[0].strip()
                t = next((x for x in targets if x.raw_line == raw_line), None)
                if t:
                    remaining_targets.append(t)

            full_country_cache: dict[str, list[dict[str, Any]]] = {}
            for t in remaining_targets:
                if not t.country_api:
                    continue
                if t.country_api in full_country_cache:
                    continue
                env_fc = await _fetch_leagues_by_country_all(client=client, limiter=limiter, country_api=t.country_api)
                full_country_cache[t.country_api] = _extract_candidates(env_fc)

            full_candidates: list[dict[str, Any]] = candidates[:]
            for _c, lst in full_country_cache.items():
                full_candidates.extend(lst)

            r4, u4 = _resolve(remaining_targets, full_candidates)
            if r4:
                resolved = resolved + [r for r in r4 if r["id"] not in {x["id"] for x in resolved}]
            unresolved = u4
    finally:
        await client.aclose()

    payload = {"resolved": resolved, "unresolved": unresolved, "ts_utc": env.get("parameters")}
    _write_yaml(Path(args.out), payload)

    print(f"[OK] resolved={len(resolved)} unresolved={len(unresolved)} -> {args.out}")
    if unresolved:
        print("[WARN] unresolved items (please adjust config/league_targets.txt wording):")
        for u in unresolved[:50]:
            print(f"  - {u}")
        if len(unresolved) > 50:
            print(f"  ... and {len(unresolved) - 50} more")

    if args.apply:
        if unresolved:
            raise SystemExit("refusing_to_apply_with_unresolved_items")
        _apply_to_configs(resolved=resolved)
        print("[OK] applied to config/jobs/daily.yaml and config/jobs/live.yaml")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))


