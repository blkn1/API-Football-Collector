from __future__ import annotations

import time

import fakeredis

from collector.delta_detector import DeltaDetector


def test_first_check_no_cache_changed_true() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    d = DeltaDetector(r)

    assert d.has_changed(1, {"status": "NS", "goals_home": 0, "goals_away": 0, "elapsed": None}) is True


def test_no_change_changed_false() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    d = DeltaDetector(r)

    state = {"status": "1H", "goals_home": 1, "goals_away": 0, "elapsed": 10}
    d.update_cache(1, state)
    assert d.has_changed(1, dict(state)) is False


def test_score_change_changed_true_and_diff() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    d = DeltaDetector(r)

    d.update_cache(1, {"status": "1H", "goals_home": 0, "goals_away": 0, "elapsed": 10})
    current = {"status": "1H", "goals_home": 1, "goals_away": 0, "elapsed": 10}

    assert d.has_changed(1, current) is True
    diff = d.get_diff(1, current)
    assert diff["goals_home"] == {"old": 0, "new": 1}


def test_status_change_changed_true() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    d = DeltaDetector(r)

    d.update_cache(1, {"status": "NS", "goals_home": None, "goals_away": None, "elapsed": None})
    assert d.has_changed(1, {"status": "1H", "goals_home": 0, "goals_away": 0, "elapsed": 1}) is True


def test_elapsed_change_changed_true() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    d = DeltaDetector(r)

    d.update_cache(1, {"status": "1H", "goals_home": 0, "goals_away": 0, "elapsed": 10})
    assert d.has_changed(1, {"status": "1H", "goals_home": 0, "goals_away": 0, "elapsed": 11}) is True


def test_cache_expiry_results_in_changed_true() -> None:
    r = fakeredis.FakeRedis(decode_responses=True)
    d = DeltaDetector(r, ttl_seconds=1)

    d.update_cache(1, {"status": "FT", "goals_home": 2, "goals_away": 1, "elapsed": 90})
    assert d.has_changed(1, {"status": "FT", "goals_home": 2, "goals_away": 1, "elapsed": 90}) is False

    time.sleep(1.05)
    assert d.has_changed(1, {"status": "FT", "goals_home": 2, "goals_away": 1, "elapsed": 90}) is True


