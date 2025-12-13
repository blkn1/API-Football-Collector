from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

from collector.api_client import APIClient  # noqa: E402


def _get_header_ci(headers: dict[str, str], key: str) -> str | None:
    for k, v in headers.items():
        if k.lower() == key.lower():
            return v
    return None


async def main() -> int:
    load_dotenv()

    try:
        client = APIClient()
    except Exception as e:
        print(f"❌ APIClient init failed: {e}")
        return 1

    try:
        result = await client.get("/status")
        await client.aclose()
    except Exception as e:
        print(f"❌ /status call failed: {e}")
        return 1

    print("✅ /status call succeeded")

    daily_limit = _get_header_ci(result.headers, "x-ratelimit-requests-limit")
    daily_remaining = _get_header_ci(result.headers, "x-ratelimit-requests-remaining")
    minute_limit = _get_header_ci(result.headers, "X-RateLimit-Limit")
    minute_remaining = _get_header_ci(result.headers, "X-RateLimit-Remaining")

    if daily_remaining is not None or minute_remaining is not None:
        print("✅ Quota headers:")
        print(f"  - daily_remaining: {daily_remaining}/{daily_limit}")
        print(f"  - minute_remaining: {minute_remaining}/{minute_limit}")
    else:
        print("❌ Quota headers not found in response")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


