from __future__ import annotations

from src.read_api.app import _extract_team_match_stats, _normalize_stat_key, _parse_intish


def test_normalize_stat_key() -> None:
    assert _normalize_stat_key("Shots on Goal") == "shots_on_goal"
    assert _normalize_stat_key("  Corner Kicks ") == "corner_kicks"
    assert _normalize_stat_key(None) == ""


def test_parse_intish_handles_percent_and_numbers() -> None:
    assert _parse_intish("55%") == 55
    assert _parse_intish(" 12 ") == 12
    assert _parse_intish(7) == 7
    assert _parse_intish(None) is None


def test_extract_team_match_stats_from_jsonb_list() -> None:
    stats = [
        {"type": "Shots on Goal", "value": 5},
        {"type": "Total Shots", "value": "14"},
        {"type": "Ball Possession", "value": "62%"},
        {"type": "Corner Kicks", "value": None},
    ]
    out = _extract_team_match_stats(stats)
    assert out["shots_on_goal"] == 5
    assert out["total_shots"] == 14
    assert out["ball_possession"] == 62
    assert out["corner_kicks"] is None


