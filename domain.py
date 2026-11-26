"""TikTok URL normalization and API request models."""

import re
from urllib.parse import urlsplit

from pydantic import BaseModel, Field


def normalize_url(value: str) -> tuple[str, str]:
    """Return a canonical URL and stable identity for a TikTok post."""
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        if host == "m.tiktok.com":
            host = "www.tiktok.com"
        if (
            parsed.scheme.lower() not in ("http", "https")
            or host not in {
                "tiktok.com", "www.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"
            }
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
        ):
            raise ValueError
        path = parsed.path.rstrip("/")
        match = re.fullmatch(r"/(?:@[^/\s]+/)?(video|photo)/(\d+)", path)
        if host in {"tiktok.com", "www.tiktok.com"} and match:
            return f"https://www.tiktok.com{path}", f"post:{match.group(2)}"
        short = host in {"vm.tiktok.com", "vt.tiktok.com"} and re.fullmatch(
            r"/[A-Za-z0-9_-]+", path
        )
        shared = host in {"tiktok.com", "www.tiktok.com"} and re.fullmatch(
            r"/t/[A-Za-z0-9_-]+", path
        )
        if short or shared:
            return f"https://{host}{path}/", f"short:{host}{path}"
    except (ValueError, TypeError):
        pass
    raise ValueError(f"Not a TikTok post or share link: {value[:160]}")


class BatchRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10000)
    request_id: str | None = Field(default=None, max_length=128)
    start_number: int = Field(ge=1, le=2147483647)


class BatchAddRequest(BaseModel):
    urls: list[str] = Field(min_length=1, max_length=10000)
    request_id: str | None = Field(default=None, max_length=128)
    batch_id: str

