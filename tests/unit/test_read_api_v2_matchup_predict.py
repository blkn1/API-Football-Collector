from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import src.read_api.app as read_api


@pytest.fixture()
def client() -> TestClient:
    return TestClient(read_api.app)


@pytest.fixture(autouse=True)
def _no_read_api_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure tests don't depend on external env auth.
    monkeypatch.delenv("READ_API_IP_ALLOWLIST", raising=False)
    monkeypatch.delenv("READ_API_BASIC_USER", raising=False)
    monkeypatch.delenv("READ_API_BASIC_PASSWORD", raising=False)


def test_v2_matchup_predict_returns_6_predictions_with_labels(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    cutoff = datetime(2026, 1, 5, 23, 59, 59, tzinfo=timezone.utc)

    # Home team (1) last 5: include one anomalous 6-0 match
    home_rows = [
        # (id, league_id, season, date, home_id, away_id, goals_home, goals_away, updated_at)
        (101, 10, 2025, cutoff, 1, 1001, 2, 1, cutoff),
        (102, 10, 2025, cutoff, 1002, 1, 0, 1, cutoff),
        (103, 10, 2025, cutoff, 1, 1003, 6, 0, cutoff),  # anomaly
        (104, 10, 2025, cutoff, 1004, 1, 1, 1, cutoff),
        (105, 10, 2025, cutoff, 1, 1005, 1, 2, cutoff),
    ]

    # Away team (2) last 5: more stable
    away_rows = [
        (201, 10, 2025, cutoff, 2001, 2, 1, 1, cutoff),
        (202, 10, 2025, cutoff, 2, 2002, 2, 0, cutoff),
        (203, 10, 2025, cutoff, 2003, 2, 0, 2, cutoff),
        (204, 10, 2025, cutoff, 2, 2004, 1, 0, cutoff),
        (205, 10, 2025, cutoff, 2005, 2, 2, 2, cutoff),
    ]

    # Forms (only a few needed); missing forms should trigger a warning
    form_rows = [
        (10, 2025, 1001, "WWDLW"),
        (10, 2025, 1002, "LLLLL"),
        (10, 2025, 2001, "DDDDD"),
    ]

    async def fake_fetchall_async(sql: str, params: tuple):
        if "FROM core.fixtures f" in sql and "status_short = ANY" in sql:
            team_id = int(params[0])
            if team_id == 1:
                return home_rows
            if team_id == 2:
                return away_rows
            return []
        if "FROM core.team_statistics" in sql:
            return form_rows
        raise AssertionError(f"Unexpected SQL: {sql}")

    # Force deterministic config (avoid relying on repo file for unit tests)
    monkeypatch.setattr(
        read_api,
        "_matchup_model_cfg",
        lambda: {
            "max_last_n": 10,
            "final_statuses": ["FT", "AET", "PEN"],
            "anomaly_z_threshold": 2.0,
            "anomaly_weight_floor": 0.3,
            "recency_half_life_matches": 3.0,
            "opponent_points_baseline": 7.0,
            "opponent_factor_min": 0.6,
            "opponent_factor_max": 1.6,
            "home_advantage": 1.10,
            "baseline_goals_per_team": 1.35,
            "lambda_min": 0.2,
            "lambda_max": 4.0,
            "max_goals": 6,
            "min_prob_unexpected": 0.02,
            "min_matches_for_split": 2,
        },
    )
    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)

    r = client.get(
        "/v2/matchup/predict?home_team_id=1&away_team_id=2&last_n=5&as_of_date=2026-01-05",
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["inputs"]["home_team_id"] == 1
    assert data["inputs"]["away_team_id"] == 2

    preds = data["predictions"]
    assert isinstance(preds, list)
    assert len(preds) == 6
    labels = [p["label"] for p in preds]
    assert labels.count("most_likely") == 1
    assert labels.count("alternative") == 2
    assert labels.count("unexpected") == 3

    # Probabilities should be within [0,1]
    for p in preds:
        assert 0.0 <= float(p["probability"]) <= 1.0


def test_v2_matchup_predict_insufficient_history_returns_400(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    async def fake_fetchall_async(sql: str, params: tuple):
        if "FROM core.fixtures f" in sql:
            return []
        return []

    monkeypatch.setattr(read_api, "_fetchall_async", fake_fetchall_async)

    r = client.get(
        "/v2/matchup/predict?home_team_id=1&away_team_id=2&last_n=5",
    )
    assert r.status_code == 400


