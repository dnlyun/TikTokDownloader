"""Application configuration read once at process startup."""

import os
from dataclasses import dataclass


def positive_env(name: str, default, cast):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


@dataclass(frozen=True)
class QueueConfig:
    max_concurrent: int = 3
    launch_interval: float = 12.0
    max_attempts: int = 4
    retry_delay: float = 5.0
    rate_limit_delay: float = 60.0

    @classmethod
    def from_env(cls) -> "QueueConfig":
        return cls(
            max_concurrent=positive_env("SSSTIK_MAX_CONCURRENT", 3, int),
            launch_interval=positive_env("SSSTIK_LAUNCH_INTERVAL", 12.0, float),
        )


QUEUE_CONFIG = QueueConfig.from_env()

