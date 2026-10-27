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

from shopify_spy.session import IMAGE, RequestFailed, SingleFlight, StoreSession

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
        self._results: SingleFlight[dict[str, Any] | None] = SingleFlight()
        self._existing: dict[str, Path] | None = None  # image name -> file, listed once

    async def process(self, item: dict[str, Any]) -> dict[str, Any]:
        """Add a ``scraped_images`` list to the item; failed downloads are omitted."""
        urls = [u for u in item.get("image_urls") or [] if isinstance(u, str)]
        results = await asyncio.gather(
            *(self._results.get(u, lambda u=u: self._download(u)) for u in urls),
            return_exceptions=True,
        )
        item[RESULT_FIELD] = [r for r in results if isinstance(r, dict)]
        return item

    def _list_existing(self) -> dict[str, Path]:
        """Map image names to files already in the folder (read once per store)."""
        if self.dir.is_dir():
            return {path.stem: path for path in self.dir.iterdir()}
        return {}

    async def _fresh(self, name: str) -> Path | None:
        if self._existing is None:
            self._existing = await asyncio.to_thread(self._list_existing)
        path = self._existing.get(name)
        if path is None:
            return None
        age_days = (time.time() - path.stat().st_mtime) / 86400
        return path if age_days < EXPIRES_DAYS else None

    async def _download(self, url: str) -> dict[str, Any] | None:
        name = image_name(url)
        existing = await self._fresh(name)
        if existing is not None:
            body = await asyncio.to_thread(existing.read_bytes)
            return _record(url, existing, body, "uptodate")
        try:
            response = await self.session.get(url, kind=IMAGE)
        except RequestFailed as exc:
            return self._failed(url, str(exc))
        if response is None:
            return self._failed(url, "forbidden by robots.txt")
        if not response.ok:
            return self._failed(url, f"HTTP {response.status}")
        if not response.body:
            return self._failed(url, "empty body")
        path = self.dir / f"{name}{_extension(url, response.headers.get('content-type'))}"
        try:
            await asyncio.to_thread(_save, path, response.body)
        except OSError as exc:
            return self._failed(url, f"can't save to {path}: {exc}")
        return _record(url, path, response.body, "downloaded")

    def _failed(self, url: str, reason: str) -> None:
        self.session.stats.images_failed += 1
        logger.warning("Image download failed: %s (%s)", url, reason)
        return None


def _save(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


def _record(url: str, path: Path, body: bytes, status: str) -> dict[str, Any]:
    return {
        "url": url,
        "path": f"full/{path.name}",
        "checksum": hashlib.md5(body).hexdigest(),
        "status": status,
    }
