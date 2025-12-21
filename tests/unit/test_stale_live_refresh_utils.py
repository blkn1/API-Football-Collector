from __future__ import annotations

from src.jobs.stale_live_refresh import _chunk


def test_chunk_respects_max_20() -> None:
    ids = list(range(1, 46))  # 45 ids
    chunks = _chunk(ids, size=20)
    assert len(chunks) == 3
    assert len(chunks[0]) == 20
    assert len(chunks[1]) == 20
    assert len(chunks[2]) == 5


