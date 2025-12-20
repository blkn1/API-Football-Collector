from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_synthetic_player_id(*, fixture_id: int, team_id: int | None, player_name: str | None, jersey_number: Any, position: Any) -> int:
    """
    API-Football sometimes returns players with missing/invalid player.id (None or 0).
    Our CORE schema keys fixture players by (fixture_id, team_id, player_id). When multiple
    players share a missing id within the same fixture/team, a bulk UPSERT fails with:
      "ON CONFLICT DO UPDATE command cannot affect row a second time"

    Fix: generate a deterministic synthetic (negative) int64 based on stable attributes,
    so the same logical row always maps to the same key, and cannot collide with real API ids.
    """
    seed = "|".join(
        [
            str(int(fixture_id)),
            str(int(team_id)) if team_id is not None else "",
            str(player_name or "").strip().lower(),
            str(jersey_number) if jersey_number is not None else "",
            str(position) if position is not None else "",
        ]
    )
    # 64-bit stable hash (use first 8 bytes). Mask to signed int63 range then make negative.
    h = hashlib.sha1(seed.encode("utf-8", errors="ignore")).digest()[:8]
    n = int.from_bytes(h, "big", signed=False) & ((1 << 63) - 1)
    if n == 0:
        n = 1
    return -int(n)


def transform_fixture_players(*, envelope: dict[str, Any], fixture_id: int) -> list[dict[str, Any]]:
    """
    GET /fixtures/players?fixture=<id> -> core.fixture_players rows
    """
    rows: list[dict[str, Any]] = []
    now = _utc_now()

    for item in envelope.get("response") or []:
        if not isinstance(item, dict):
            continue
        team = item.get("team") or {}
        players = item.get("players") or []
        try:
            team_id = int(team.get("id")) if team.get("id") is not None else None
        except Exception:
            team_id = None

        for p in players:
            if not isinstance(p, dict):
                continue
            player = p.get("player") or {}
            stats = p.get("statistics")
            try:
                player_id = int(player.get("id")) if player.get("id") is not None else None
            except Exception:
                player_id = None

            # Normalize missing/invalid ids to deterministic synthetic ids (negative).
            if player_id in (None, 0):
                # Try to incorporate jersey number/position if available (usually in statistics[0].games).
                jersey_number = None
                position = None
                if isinstance(stats, list) and stats:
                    s0 = stats[0] if isinstance(stats[0], dict) else {}
                    games = s0.get("games") if isinstance(s0, dict) else None
                    if isinstance(games, dict):
                        jersey_number = games.get("number")
                        position = games.get("position")
                player_id = _stable_synthetic_player_id(
                    fixture_id=int(fixture_id),
                    team_id=team_id,
                    player_name=player.get("name"),
                    jersey_number=jersey_number,
                    position=position,
                )

            rows.append(
                {
                    "fixture_id": int(fixture_id),
                    "team_id": team_id,
                    "player_id": player_id,
                    "player_name": player.get("name"),
                    "statistics": stats,
                    "update_utc": now,
                    "created_at": now,
                    "updated_at": now,
                }
            )

    # Defensive dedup: ensure each (fixture_id, team_id, player_id) appears at most once per batch.
    # (Even with synthetic ids, this avoids any edge-case duplication from the source payload.)
    dedup: dict[tuple[int, int | None, int], dict[str, Any]] = {}
    for r in rows:
        try:
            key = (int(r["fixture_id"]), (int(r["team_id"]) if r.get("team_id") is not None else None), int(r["player_id"]))
        except Exception:
            continue
        dedup[key] = r
    return list(dedup.values())


def transform_fixture_statistics(*, envelope: dict[str, Any], fixture_id: int) -> list[dict[str, Any]]:
    """
    GET /fixtures/statistics?fixture=<id> -> core.fixture_statistics rows
    """
    rows: list[dict[str, Any]] = []
    now = _utc_now()

    for item in envelope.get("response") or []:
        if not isinstance(item, dict):
            continue
        team = item.get("team") or {}
        stats = item.get("statistics")
        try:
            team_id = int(team.get("id")) if team.get("id") is not None else None
        except Exception:
            team_id = None
        rows.append(
            {
                "fixture_id": int(fixture_id),
                "team_id": team_id,
                "statistics": stats,
                "update_utc": now,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def transform_fixture_lineups(*, envelope: dict[str, Any], fixture_id: int) -> list[dict[str, Any]]:
    """
    GET /fixtures/lineups?fixture=<id> -> core.fixture_lineups rows
    """
    rows: list[dict[str, Any]] = []
    now = _utc_now()

    for item in envelope.get("response") or []:
        if not isinstance(item, dict):
            continue
        team = item.get("team") or {}
        try:
            team_id = int(team.get("id")) if team.get("id") is not None else None
        except Exception:
            team_id = None

        rows.append(
            {
                "fixture_id": int(fixture_id),
                "team_id": team_id,
                "formation": item.get("formation"),
                "start_xi": item.get("startXI"),
                "substitutes": item.get("substitutes"),
                "coach": item.get("coach"),
                "colors": item.get("colors"),
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


def _event_key(*, fixture_id: int, elapsed: int | None, extra: int | None, team_id: int | None, player_id: int | None, assist_id: int | None, type_: str | None, detail: str | None, comments: str | None, fallback_index: int) -> str:
    base = "|".join(
        [
            str(int(fixture_id)),
            str(elapsed) if elapsed is not None else "",
            str(extra) if extra is not None else "",
            str(team_id) if team_id is not None else "",
            str(player_id) if player_id is not None else "",
            str(assist_id) if assist_id is not None else "",
            (type_ or "").strip().lower(),
            (detail or "").strip().lower(),
            (comments or "").strip().lower(),
            str(int(fallback_index)),
        ]
    )
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def transform_fixture_events(*, envelope: dict[str, Any], fixture_id: int) -> list[dict[str, Any]]:
    """
    GET /fixtures/events?fixture=<id> -> core.fixture_events rows
    """
    rows: list[dict[str, Any]] = []
    now = _utc_now()

    for idx, item in enumerate(envelope.get("response") or []):
        if not isinstance(item, dict):
            continue
        time_obj = item.get("time") or {}
        team = item.get("team") or {}
        player = item.get("player") or {}
        assist = item.get("assist") or {}

        elapsed = time_obj.get("elapsed")
        extra = time_obj.get("extra")
        try:
            elapsed_i = int(elapsed) if elapsed is not None else None
        except Exception:
            elapsed_i = None
        try:
            extra_i = int(extra) if extra is not None else None
        except Exception:
            extra_i = None
        try:
            team_id = int(team.get("id")) if team.get("id") is not None else None
        except Exception:
            team_id = None
        try:
            player_id = int(player.get("id")) if player.get("id") is not None else None
        except Exception:
            player_id = None
        try:
            assist_id = int(assist.get("id")) if assist.get("id") is not None else None
        except Exception:
            assist_id = None

        type_ = item.get("type")
        detail = item.get("detail")
        comments = item.get("comments")

        ek = _event_key(
            fixture_id=int(fixture_id),
            elapsed=elapsed_i,
            extra=extra_i,
            team_id=team_id,
            player_id=player_id,
            assist_id=assist_id,
            type_=str(type_) if type_ is not None else None,
            detail=str(detail) if detail is not None else None,
            comments=str(comments) if comments is not None else None,
            fallback_index=idx,
        )

        rows.append(
            {
                "fixture_id": int(fixture_id),
                "event_key": ek,
                "time_elapsed": elapsed_i,
                "time_extra": extra_i,
                "team_id": team_id,
                "player_id": player_id,
                "assist_id": assist_id,
                "type": type_,
                "detail": detail,
                "comments": comments,
                "raw": item,
                "created_at": now,
                "updated_at": now,
            }
        )
    return rows


