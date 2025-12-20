from __future__ import annotations

from src.transforms.fixture_endpoints import transform_fixture_players


def test_transform_fixture_players_generates_deterministic_synthetic_ids_for_missing_or_zero() -> None:
    fixture_id = 999
    envelope = {
        "response": [
            {
                "team": {"id": 123},
                "players": [
                    # Missing id
                    {
                        "player": {"id": None, "name": "Player A"},
                        "statistics": [{"games": {"number": 1, "position": "G"}}],
                    },
                    # Explicit zero id
                    {
                        "player": {"id": 0, "name": "Player B"},
                        "statistics": [{"games": {"number": 2, "position": "D"}}],
                    },
                ],
            }
        ]
    }

    rows1 = transform_fixture_players(envelope=envelope, fixture_id=fixture_id)
    rows2 = transform_fixture_players(envelope=envelope, fixture_id=fixture_id)

    assert len(rows1) == 2
    assert len(rows2) == 2

    ids1 = sorted([int(r["player_id"]) for r in rows1])
    ids2 = sorted([int(r["player_id"]) for r in rows2])

    # Synthetic IDs should be negative and deterministic.
    assert all(i < 0 for i in ids1)
    assert ids1 == ids2

    # Ensure uniqueness within the same fixture/team (prevents bulk UPSERT conflicts).
    assert len(set(ids1)) == 2


