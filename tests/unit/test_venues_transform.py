from __future__ import annotations

import json
from pathlib import Path

from transforms.venues import transform_venues


def test_transform_venues() -> None:
    p = Path(__file__).resolve().parents[1] / "fixtures" / "api_responses" / "venues_556.json"
    env = json.loads(p.read_text(encoding="utf-8"))
    rows = transform_venues(env)
    assert rows == [
        {
            "id": 556,
            "name": "Old Trafford",
            "address": "Sir Matt Busby Way",
            "city": "Manchester",
            "country": "England",
            "capacity": 76212,
            "surface": "grass",
            "image": "https://media.api-sports.io/football/venues/556.png",
        }
    ]


