from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_date(value: Any) -> date | None:
    """
    Injuries payloads vary a bit; accept:
    - YYYY-MM-DD
    - ISO datetime string
    """
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # date-only
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            try:
                return date.fromisoformat(s)
            except Exception:
                return None
        # datetime-ish
        try:
            s2 = s[:-1] + "+00:00" if s.endswith("Z") else s
            dt = datetime.fromisoformat(s2)
            dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).date()
        except Exception:
            return None
    return None


def _injury_key(*, league_id: int, season: int, team_id: int | None, player_id: int | None, d: date | None, type_: str | None, reason: str | None, severity: str | None) -> str:
    base = "|".join(
        [
            str(int(league_id)),
            str(int(season)),
            str(int(team_id)) if team_id is not None else "",
            str(int(player_id)) if player_id is not None else "",
            d.isoformat() if d else "",
            (type_ or "").strip().lower(),
            (reason or "").strip().lower(),
            (severity or "").strip().lower(),
        ]
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def transform_injuries(*, envelope: dict[str, Any], league_id: int, season: int) -> list[dict[str, Any]]:
    """
    RAW envelope (/injuries) -> CORE rows for core.injuries
    """
    rows: list[dict[str, Any]] = []
    now = _utc_now()

    for item in envelope.get("response") or []:
        if not isinstance(item, dict):
            continue
        league = item.get("league") or {}
        team = item.get("team") or {}
        player = item.get("player") or {}
        fixture = item.get("fixture") or {}

        try:
            team_id = int(team.get("id")) if team.get("id") is not None else None
        except Exception:
            team_id = None
        try:
            player_id = int(player.get("id")) if player.get("id") is not None else None
        except Exception:
            player_id = None

        type_ = player.get("type") or item.get("type")
        reason = player.get("reason") or item.get("reason")
        severity = player.get("severity") or item.get("severity")

        d = _parse_date(fixture.get("date") or player.get("date") or item.get("date"))

        ik = _injury_key(
            league_id=int(league_id),
            season=int(season),
            team_id=team_id,
            player_id=player_id,
            d=d,
            type_=str(type_) if type_ is not None else None,
            reason=str(reason) if reason is not None else None,
            severity=str(severity) if severity is not None else None,
        )

        rows.append(
            {
                "league_id": int((league.get("id") or league_id) or league_id),
                "season": int((league.get("season") or season) or season),
                "injury_key": ik,
                "team_id": team_id,
                "player_id": player_id,
                "player_name": player.get("name"),
                "team_name": team.get("name"),
                "type": type_,
                "reason": reason,
                "severity": severity,
                "date": d,
                "timezone": fixture.get("timezone") or league.get("timezone"),
                "raw": item,
                "created_at": now,
                "updated_at": now,
            }
        )

    return rows


