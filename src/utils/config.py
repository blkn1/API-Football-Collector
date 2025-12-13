from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import os
import yaml


@dataclass(frozen=True)
class APIConfig:
    base_url: str
    api_key_env: str
    timeout_seconds: float
    default_timezone: str = "UTC"


@dataclass(frozen=True)
class RateLimiterConfig:
    # NOTE: token_bucket_per_minute is the hard ceiling; minute_soft_limit is the safe working cap.
    token_bucket_per_minute: int
    minute_soft_limit: int
    daily_limit: int
    emergency_stop_threshold: int


def _project_root() -> Path:
    # Resolve from this file: .../src/utils/config.py -> project root is 3 parents up.
    return Path(__file__).resolve().parents[2]


def load_api_config(path: str | None = None) -> APIConfig:
    """
    Load API config from YAML.

    Precedence:
    - explicit `path`
    - env `API_FOOTBALL_API_CONFIG`
    - project default `config/api.yaml`
    """
    cfg_path = Path(path or os.getenv("API_FOOTBALL_API_CONFIG") or (_project_root() / "config" / "api.yaml"))
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    api = cfg.get("api") or {}

    base_url = api.get("base_url")
    api_key_env = api.get("api_key_env")
    timeout_seconds = api.get("timeout_seconds")
    default_tz = api.get("default_timezone") or "UTC"

    if not base_url:
        raise ValueError(f"Missing api.base_url in {cfg_path}")
    if not api_key_env:
        raise ValueError(f"Missing api.api_key_env in {cfg_path}")
    if timeout_seconds is None:
        raise ValueError(f"Missing api.timeout_seconds in {cfg_path}")

    return APIConfig(
        base_url=str(base_url),
        api_key_env=str(api_key_env),
        timeout_seconds=float(timeout_seconds),
        default_timezone=str(default_tz),
    )


def load_rate_limiter_config(path: str | None = None) -> RateLimiterConfig:
    """
    Load rate limiter config from YAML.

    Precedence:
    - explicit `path`
    - env `API_FOOTBALL_RATE_LIMITER_CONFIG`
    - project default `config/rate_limiter.yaml`
    """
    cfg_path = Path(path or os.getenv("API_FOOTBALL_RATE_LIMITER_CONFIG") or (_project_root() / "config" / "rate_limiter.yaml"))
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    rl = cfg.get("rate_limiter") or {}

    token_bucket = rl.get("token_bucket_per_minute")
    minute_soft = rl.get("minute_soft_limit")
    daily_limit = rl.get("daily_limit")
    emergency = rl.get("emergency_stop_threshold")

    missing: list[str] = []
    if token_bucket is None:
        missing.append("rate_limiter.token_bucket_per_minute")
    if minute_soft is None:
        missing.append("rate_limiter.minute_soft_limit")
    if daily_limit is None:
        missing.append("rate_limiter.daily_limit")
    if emergency is None:
        missing.append("rate_limiter.emergency_stop_threshold")
    if missing:
        raise ValueError(f"Missing {', '.join(missing)} in {cfg_path}")

    return RateLimiterConfig(
        token_bucket_per_minute=int(token_bucket),
        minute_soft_limit=int(minute_soft),
        daily_limit=int(daily_limit),
        emergency_stop_threshold=int(emergency),
    )


def load_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


