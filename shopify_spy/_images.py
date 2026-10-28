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
    (b"II*\x00", ".tiff"),
    (b"MM\x00*", ".tiff"),
    (b"II+\x00", ".tiff"),  # BigTIFF
    (b"MM\x00+", ".tiff"),
    (b"\xff\x0a", ".jxl"),  # JPEG XL codestream
    (b"\x00\x00\x00\x0cJXL \r\n\x87\n", ".jxl"),  # JPEG XL container
    (b"\x00\x00\x00\x0cjP  \r\n\x87\n", ".jp2"),  # JPEG 2000
)
# AVIF and HEIF share the ftyp box of MP4 video; only its brands say it's an image.
# Checked in this order, since a HEIC or AVIF file may also list the generic mif1.
FTYP_BRANDS = {
    b"avif": ".avif", b"avis": ".avif",
    b"heic": ".heic", b"heix": ".heic", b"hevc": ".heic", b"hevx": ".heic",
    b"mif1": ".heif", b"msf1": ".heif",
}  # fmt: skip
FTYP_MAX = 256  # ftyp boxes are a few dozen bytes; don't scan a corrupt size field's worth
# A BMP's "BM" is followed by sizes; the DIB header's own size tells it from text
BMP_HEADER_SIZES = {12, 40, 52, 56, 64, 108, 124}


def image_name(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def image_extension(body: bytes) -> str | None:
    """The extension for the image format the body starts with; None if it isn't one."""
    if body[4:8] == b"ftyp":  # before ICO, whose signature is also a 256-byte box's size
        return _ftyp_extension(body)
    for signature, ext in SIGNATURES:
        if body.startswith(signature):
            return ext
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return ".webp"
    if body.startswith(b"BM") and int.from_bytes(body[14:18], "little") in BMP_HEADER_SIZES:
        return ".bmp"
    if body.startswith(b"\x00\x00\x01\x00") and body[4:6] not in (b"", b"\x00\x00"):
        return ".ico"  # followed by a nonzero image count
    return None


def _ftyp_extension(body: bytes) -> str | None:
    size, start = int.from_bytes(body[:4], "big"), 8
    if size == 1:  # a 64-bit size follows the box type
        size, start = int.from_bytes(body[8:16], "big"), 16
    elif size == 0:  # the box runs to the end of the file
        size = len(body)
    end = min(size, FTYP_MAX)
    # Major brand, a 4-byte minor version, then compatible brands
    brands = {body[start : start + 4]}
    brands |= {body[i : i + 4] for i in range(start + 8, end - 3, 4)}
    return next((ext for brand, ext in FTYP_BRANDS.items() if brand in brands), None)


class ImageDownloader:
    """Downloads an item's images into ``<images_dir>/full/``.

    Each URL is downloaded once per store; files younger than 90 days are reused.
    """

    def __init__(self, session: StoreSession, images_dir: Path) -> None:
        self.session = session
        self.dir = images_dir / "full"
        self._results: SingleFlight[dict[str, Any] | None] = SingleFlight()
        self._existing: dict[str, list[Path]] | None = None  # image name -> files, listed once

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

    def _list_existing(self) -> dict[str, list[Path]]:
        """Group the files already in the folder by image name (read once per store)."""
        existing: dict[str, list[Path]] = {}
        if self.dir.is_dir():
            for path in self.dir.iterdir():
                existing.setdefault(path.stem, []).append(path)
        return existing

    async def _cached(self, url: str, name: str) -> tuple[Path, bytes] | None:
        """A cached copy of the image younger than 90 days, if there is one.

        A cached file counts only if its bytes are the format its extension names. A
        ``.jpg`` counts only for a JPEG URL, since the Scrapy-era pipeline saved every
        image as a re-encoded JPEG under the same SHA-1 name.
        """
        if self._existing is None:
            self._existing = await asyncio.to_thread(self._list_existing)
        jpeg_url = Path(urllib.parse.urlsplit(url).path).suffix.lower() in (".jpg", ".jpeg")
        for path in self._existing.get(name, ()):
            if path.suffix == ".jpg" and not jpeg_url:
                continue
            try:
                if (time.time() - path.stat().st_mtime) / 86400 >= EXPIRES_DAYS:
                    continue
                body = await asyncio.to_thread(path.read_bytes)
            except OSError:
                continue
            if image_extension(body) == path.suffix:
                return path, body
        return None

    async def _download(self, url: str) -> dict[str, Any] | None:
        name = image_name(url)
        cached = await self._cached(url, name)
        if cached is not None:
            return _record(url, *cached, "uptodate")
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
            content_type = response.headers.get("content-type") or "none"
            return self._failed(
                url,
                f"unsupported format: Content-Type {content_type}, starts {response.body[:16]!r}",
            )
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
    """Write via a temporary file, so an interrupted save never leaves a partial image."""
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.part")
    partial.write_bytes(body)
    partial.replace(path)


def _record(url: str, path: Path, body: bytes, status: str) -> dict[str, Any]:
    return {
        "url": url,
        "path": f"full/{path.name}",
        "checksum": hashlib.md5(body).hexdigest(),
        "status": status,
    }
