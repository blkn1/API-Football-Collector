from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

from collector.api_client import APIClient


@pytest.mark.asyncio
async def test_status_endpoint():
    # Real API call (FREE endpoint; does not consume quota)
    load_dotenv()
    assert os.getenv("API_FOOTBALL_KEY"), "API_FOOTBALL_KEY must be set in .env"

    client = APIClient()
    result = await client.get("/status")
    await client.aclose()

    assert result.status_code in (200, 204)
    # quota headers should exist even on /status
    assert (
        "x-ratelimit-requests-remaining" in result.headers
        or "X-RateLimit-Remaining" in result.headers
    )


@pytest.mark.asyncio
async def test_header_only():
    client = APIClient()
    with pytest.raises(ValueError):
        await client.request("GET", "/status", headers={"x-test": "nope"})
    await client.aclose()


@pytest.mark.asyncio
async def test_get_only():
    client = APIClient()
    with pytest.raises(ValueError):
        await client.request("POST", "/status")
    await client.aclose()


