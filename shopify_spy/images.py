"""Image downloads: original bytes, named by the SHA-1 of the URL, cached for 90 days."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import time
import urllib.parse
from pathlib import Path
from typing import Any

from shopify_spy.session import RequestFailed, StoreSession

logger = logging.getLogger("shopify_spy")

EXPIRES_DAYS = 90
RESULT_FIELD = "scraped_images"
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".svg", ".bmp", ".tif", ".tiff", ".ico",
}  # fmt: skip


def image_name(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def _extension(url: str, content_type: str | None) -> str:
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    if suffix in IMAGE_EXTENSIONS:
        return suffix
    if content_type:
        guessed = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if guessed:
            return ".jpg" if guessed == ".jpe" else guessed
    return ".jpg"


class ImageDownloader:
    """Downloads an item's images into ``<images_dir>/full/``.

    Each URL is downloaded once per store; files younger than 90 days are reused.
    """

    def __init__(self, session: StoreSession, images_dir: Path) -> None:
        self.session = session
        self.dir = images_dir / "full"
        self._results: dict[str, asyncio.Future[dict[str, Any] | None]] = {}

    async def process(self, item: dict[str, Any]) -> dict[str, Any]:
        """Add a ``scraped_images`` list to the item; failed downloads are omitted."""
        urls = [u for u in item.get("image_urls") or [] if isinstance(u, str)]
        if not urls:
            item[RESULT_FIELD] = []
            return item
        results = await asyncio.gather(*(self._get(u) for u in urls))
        item[RESULT_FIELD] = [r for r in results if r is not None]
        return item

    async def _get(self, url: str) -> dict[str, Any] | None:
        future = self._results.get(url)
        if future is None:
            future = self._results[url] = asyncio.get_running_loop().create_future()
            try:
                result = await self._download(url)
            except BaseException:
                future.set_result(None)
                raise
            future.set_result(result)
        return await asyncio.shield(future)

    def _fresh(self, name: str) -> Path | None:
        for path in self.dir.glob(f"{name}.*"):
            age_days = (time.time() - path.stat().st_mtime) / 86400
            if age_days < EXPIRES_DAYS:
                return path
        return None

    async def _download(self, url: str) -> dict[str, Any] | None:
        name = image_name(url)
        existing = self._fresh(name)
        if existing is not None:
            return _record(url, existing, existing.read_bytes(), "uptodate")
        try:
            response = await self.session.get(url, kind="image", dedupe=False)
        except RequestFailed as exc:
            return self._failed(url, str(exc))
        if response is None:
            return self._failed(url, "forbidden by robots.txt")
        if not 200 <= response.status < 300:
            return self._failed(url, f"HTTP {response.status}")
        if not response.body:
            return self._failed(url, "empty body")
        path = self.dir / f"{name}{_extension(url, response.headers.get('content-type'))}"
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(response.body)
        return _record(url, path, response.body, "downloaded")

    def _failed(self, url: str, reason: str) -> None:
        self.session.stats.images_failed += 1
        logger.warning("Image download failed: %s (%s)", url, reason)
        return None


def _record(url: str, path: Path, body: bytes, status: str) -> dict[str, Any]:
    return {
        "url": url,
        "path": f"full/{path.name}",
        "checksum": hashlib.md5(body).hexdigest(),
        "status": status,
    }
