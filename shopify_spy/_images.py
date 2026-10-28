"""Image downloads: original bytes, named by the SHA-1 of the URL, cached for 90 days."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any

from shopify_spy._session import (
    IMAGE,
    RequestFailed,
    ShutdownRequested,
    SingleFlight,
    StoreSession,
)

logger = logging.getLogger("shopify_spy")

EXPIRES_DAYS = 90
RESULT_FIELD = "scraped_images"

# Leading bytes of each image format stores serve, and the extension it's saved under
SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),
    (b"\x00\x00\x01\x00", ".ico"),
    (b"\xff\x0a", ".jxl"),  # JPEG XL codestream
    (b"\x00\x00\x00\x0cJXL \r\n\x87\n", ".jxl"),  # JPEG XL container
)
# AVIF and HEIF share the ftyp box of MP4 video; only its brands say it's an image.
# Checked in this order, since a HEIC or AVIF file may also list the generic mif1.
FTYP_BRANDS = {
    b"avif": ".avif", b"avis": ".avif",
    b"heic": ".heic", b"heix": ".heic", b"hevc": ".heic", b"hevx": ".heic",
    b"mif1": ".heif", b"msf1": ".heif",
}  # fmt: skip
FTYP_MAX = 256  # ftyp boxes are a few dozen bytes; don't scan a corrupt size field's worth
# URL suffixes whose cached file can be reused, mapped to the extension it was saved under
URL_SUFFIXES = {
    ".jpg": ".jpg", ".jpeg": ".jpg", ".png": ".png", ".gif": ".gif", ".webp": ".webp",
    ".avif": ".avif", ".heic": ".heic", ".heif": ".heif", ".bmp": ".bmp",
    ".tif": ".tiff", ".tiff": ".tiff", ".ico": ".ico", ".jxl": ".jxl",
}  # fmt: skip


def image_name(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def image_extension(body: bytes) -> str | None:
    """The extension for the image format the body starts with; None if it isn't one."""
    for signature, ext in SIGNATURES:
        if body.startswith(signature):
            return ext
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return ".webp"
    if body[4:8] == b"ftyp":
        end = min(int.from_bytes(body[:4], "big"), FTYP_MAX)
        brands = {body[8:12]} | {body[i : i + 4] for i in range(16, end - 3, 4)}
        return next((ext for brand, ext in FTYP_BRANDS.items() if brand in brands), None)
    return None


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
        records = []
        for url, result in zip(urls, results, strict=True):
            if isinstance(result, dict):
                records.append(result)
            elif isinstance(result, Exception) and not isinstance(result, ShutdownRequested):
                self._failed(url, repr(result))
            elif isinstance(result, BaseException) and not isinstance(result, Exception):
                raise result  # cancellation
        item[RESULT_FIELD] = records
        return item

    def _list_existing(self) -> dict[str, Path]:
        """Map file names to files already in the folder (read once per store)."""
        if self.dir.is_dir():
            return {path.name: path for path in self.dir.iterdir()}
        return {}

    async def _fresh(self, url: str, name: str) -> Path | None:
        """A cached copy of the image younger than 90 days, if there is one.

        Only a file with the extension the URL implies counts, so JPEGs re-encoded by
        the Scrapy-era pipeline (same SHA-1 names) aren't reused for other formats.
        URLs without an image extension, or whose image is in another format, aren't
        served from the cache.
        """
        suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
        ext = URL_SUFFIXES.get(suffix)
        if ext is None:
            return None
        if self._existing is None:
            self._existing = await asyncio.to_thread(self._list_existing)
        path = self._existing.get(f"{name}{ext}")
        if path is None:
            return None
        age_days = (time.time() - path.stat().st_mtime) / 86400
        return path if age_days < EXPIRES_DAYS else None

    async def _download(self, url: str) -> dict[str, Any] | None:
        name = image_name(url)
        existing = await self._fresh(url, name)
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
        ext = image_extension(response.body)
        if ext is None:
            return self._failed(url, "not an image in a known format")
        path = self.dir / f"{name}{ext}"
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
