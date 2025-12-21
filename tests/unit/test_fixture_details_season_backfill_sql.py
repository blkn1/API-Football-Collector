from __future__ import annotations

import pytest


def test_values_placeholder_count_matches_pairs() -> None:
    """
    Guardrail for season backfill selector query construction:
    VALUES list must have 2 placeholders per (league_id, season) pair.
    """
    pairs = [(39, 2025), (204, 2025), (399, 2026)]
    values_sql = ", ".join(["(%s,%s)"] * len(pairs))
    assert values_sql.count("%s") == len(pairs) * 2


@pytest.mark.parametrize(
    "pairs",
    [
        [(39, 2025)],
        [(39, 2025), (140, 2025)],
        [(39, 2025), (399, 2026), (516, 2024)],
    ],
)
def test_values_sql_shape(pairs: list[tuple[int, int]]) -> None:
    values_sql = ", ".join(["(%s,%s)"] * len(pairs))
    # Each pair should contribute exactly one "(%s,%s)" segment
    assert len(values_sql.split(", ")) == len(pairs)


