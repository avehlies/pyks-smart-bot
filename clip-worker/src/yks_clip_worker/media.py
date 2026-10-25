"""Downloading clips to temporary files."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx


class DownloadError(Exception):
    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent  # retrying won't help (e.g. the file is too big)


async def download(http: httpx.AsyncClient, url: str, directory: Path, *, max_bytes: int) -> Path:
    """Stream `url` into `directory`, refusing files over `max_bytes`."""
    suffix = PurePosixPath(urlparse(url).path).suffix[:10]
    target = directory / f"clip{suffix}"
    written = 0
    try:
        async with http.stream("GET", url) as response:
            if response.status_code in (403, 404, 410):
                raise DownloadError(f"Download failed: HTTP {response.status_code}")
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
