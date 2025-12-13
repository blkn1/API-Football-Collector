from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv


class APIClientError(Exception):
    pass


class AuthenticationError(APIClientError):
    pass


class RateLimitError(APIClientError):
    pass


class APITimeoutError(APIClientError):
    pass


class APIServerError(APIClientError):
    pass


class APIUnexpectedStatusError(APIClientError):
    def __init__(self, status_code: int, body_text: str | None = None) -> None:
        super().__init__(f"Unexpected status code: {status_code}")
        self.status_code = status_code
        self.body_text = body_text


@dataclass(frozen=True)
class APIResult:
    status_code: int
    data: dict[str, Any] | None
    headers: dict[str, str]


class APIClient:
    """
    API-Football client (Phase 1)
    - GET-only
    - Exactly one auth header: x-apisports-key
    - Async httpx
    """

    def __init__(
        self,
        *,
        base_url: str = "https://v3.football.api-sports.io",
        timeout_seconds: float = 30.0,
        api_key_env: str = "API_FOOTBALL_KEY",
    ) -> None:
        load_dotenv()
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise ValueError(f"Missing API key env var: {api_key_env}")

        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout_seconds)
        self._api_key = api_key

        # IMPORTANT: do not set any additional custom headers.
        # httpx will still send mandatory HTTP headers (Host, etc.).
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            headers={},  # avoid adding any custom headers at client level
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> APIResult:
        return await self.request("GET", endpoint, params=params)

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> APIResult:
        # GET only enforcement
        if method.upper() != "GET":
            raise ValueError("GET only: POST/PUT/DELETE are forbidden for API-Football")

        # Header enforcement: ONLY x-apisports-key allowed as a custom header
        if headers:
            raise ValueError("Custom headers are forbidden. APIClient sets only 'x-apisports-key'.")

        # Ensure endpoint starts with /
        if not endpoint.startswith("/"):
            endpoint = f"/{endpoint}"

        req_headers = {"x-apisports-key": self._api_key}

        try:
            resp = await self._client.request(
                method="GET",
                url=endpoint,
                params=params or {},
                headers=req_headers,
            )
        except httpx.TimeoutException as e:
            raise APITimeoutError("Request timeout") from e
        except httpx.RequestError as e:
            raise APIClientError(f"Request error: {e}") from e

        # Quota tracking: return headers
        resp_headers = {k: v for k, v in resp.headers.items()}

        # Status handling
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError as e:
                raise APIClientError("Failed to parse JSON") from e
            return APIResult(status_code=200, data=data, headers=resp_headers)

        if resp.status_code == 204:
            return APIResult(status_code=204, data=None, headers=resp_headers)

        if resp.status_code == 401:
            raise AuthenticationError("Unauthorized (401): invalid API key")

        if resp.status_code == 429:
            raise RateLimitError("Too Many Requests (429): rate limit exceeded")

        if resp.status_code == 499:
            raise APITimeoutError("API timeout (499)")

        if resp.status_code in (500, 502, 504):
            raise APIServerError(f"API server error ({resp.status_code})")

        body_text: str | None
        try:
            body_text = resp.text
        except Exception:
            body_text = None
        raise APIUnexpectedStatusError(resp.status_code, body_text=body_text)


