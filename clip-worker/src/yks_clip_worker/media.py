"""Downloading clips to temporary files."""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlparse

import httpx

# Statuses the Discord CDN gives for an expired (or removed) attachment link.
GONE_STATUSES = frozenset({403, 404, 410})


class DownloadError(Exception):
    def __init__(self, message: str, *, permanent: bool = False, gone: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent  # retrying won't help (e.g. the file is too big)
        self.gone = gone  # the link was refused: it expired or the file was removed


def link_expires_at(url: str) -> float | None:
    """When a signed Discord CDN link stops working (unix time), from its `ex=<hex>` parameter.
    None if the URL has no readable expiry."""
    values = parse_qs(urlparse(url).query).get("ex")
    if not values:
        return None
    try:
        return float(int(values[0], 16))
    except ValueError:
        return None


def link_is_fresh(url: str, *, margin: float = 600.0, now: float | None = None) -> bool:
    """Will this link still work for at least `margin` more seconds?"""
    expires = link_expires_at(url)
    current = time.time() if now is None else now
    return expires is not None and expires - margin > current


async def download(http: httpx.AsyncClient, url: str, directory: Path, *, max_bytes: int) -> Path:
    """Stream `url` into `directory`, refusing files over `max_bytes`."""
    suffix = PurePosixPath(urlparse(url).path).suffix[:10]
    target = directory / f"clip{suffix}"
    written = 0
    try:
        async with http.stream("GET", url) as response:
            if response.status_code in GONE_STATUSES:
                raise DownloadError(f"Download failed: HTTP {response.status_code}", gone=True)
            response.raise_for_status()
            if int(response.headers.get("content-length") or 0) > max_bytes:
                raise DownloadError("Clip is larger than MAX_DOWNLOAD_MB", permanent=True)
            with target.open("wb") as out:
                async for chunk in response.aiter_bytes():
                    written += len(chunk)
                    if written > max_bytes:
                        raise DownloadError("Clip is larger than MAX_DOWNLOAD_MB", permanent=True)
                    out.write(chunk)
    except httpx.HTTPError as exc:
        raise DownloadError(f"Download failed: {exc}") from exc
    if written == 0:
        raise DownloadError("Downloaded an empty file")
    return target
