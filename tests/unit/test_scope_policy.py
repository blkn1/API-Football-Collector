from __future__ import annotations

from pathlib import Path

import pytest

from src.utils.scope_policy import decide_scope, load_scope_policy


def test_scope_policy_cup_disables_standings_by_default(tmp_path: Path):
    p = tmp_path / "scope_policy.yaml"
    p.write_text(
        "\n".join(
            [
                "version: 1",
                "baseline_enabled_endpoints: [/fixtures]",
                "by_competition_type:",
                "  Cup:",
                "    disabled_endpoints: [/standings]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    policy = load_scope_policy(p)

    d = decide_scope(
        league_id=206,
        season=2025,
        endpoint="/standings",
        policy=policy,
        league_type_provider=lambda _lid: "Cup",
    )
    assert d.in_scope is False
    assert "Cup" in d.reason


def test_scope_policy_league_allows_standings(tmp_path: Path):
    p = tmp_path / "scope_policy.yaml"
    p.write_text(
        "\n".join(
            [
                "version: 1",
                "baseline_enabled_endpoints: [/fixtures]",
                "by_competition_type:",
                "  League:",
                "    enabled_endpoints: [/standings]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    policy = load_scope_policy(p)

    d = decide_scope(
        league_id=78,
        season=2025,
        endpoint="/standings",
        policy=policy,
        league_type_provider=lambda _lid: "League",
    )
    assert d.in_scope is True


def test_scope_policy_unknown_type_fails_open(tmp_path: Path):
    p = tmp_path / "scope_policy.yaml"
    p.write_text("version: 1\nbaseline_enabled_endpoints: []\n", encoding="utf-8")
    policy = load_scope_policy(p)

    d = decide_scope(
        league_id=9999,
        season=2025,
        endpoint="/standings",
        policy=policy,
        league_type_provider=lambda _lid: None,
    )
    assert d.in_scope is True
    assert d.reason == "league_type_unknown_fail_open"


def test_scope_policy_override_wins(tmp_path: Path):
    p = tmp_path / "scope_policy.yaml"
    p.write_text(
        "\n".join(
            [
                "version: 1",
                "baseline_enabled_endpoints: []",
                "by_competition_type:",
                "  League:",
                "    enabled_endpoints: [/standings]",
                "overrides:",
                "  - league_id: 206",
                "    season: 2025",
                "    disabled_endpoints: [/standings]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    policy = load_scope_policy(p)

    d = decide_scope(
        league_id=206,
        season=2025,
        endpoint="/standings",
        policy=policy,
        league_type_provider=lambda _lid: "League",
    )
    assert d.in_scope is False
    assert d.reason == "override_disabled"


