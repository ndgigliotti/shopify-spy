"""Image downloads (plan §3.6 as amended by D1/D13, §4.9)."""

import hashlib
import os
import time

import pytest

from shopify_spy._images import image_extension
from tests.flow_support import bulk_url, redirect, scrape

PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"
WEBP = b"RIFF\x00\x00\x00\x00WEBPfake"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def ftyp(major, *compatible):
    """An ISO media file's leading ftyp box, as AVIF, HEIF and MP4 files start."""
    brands = major + b"\x00\x00\x00\x00" + b"".join(compatible)
    return (8 + len(brands)).to_bytes(4, "big") + b"ftyp" + brands + b"\x00\x00\x00\x08meta"


def sha1(url):
    return hashlib.sha1(url.encode()).hexdigest()


def md5(data):
    return hashlib.md5(data).hexdigest()


def shop(*image_lists):
    """Bulk route with one product per list of image URLs."""
    products = [
        {"id": i, "handle": f"p{i}", "images": [{"src": u} for u in urls]}
        for i, urls in enumerate(image_lists)
    ]
    return {bulk_url(1): {"products": products}}


def run_images(routes, images_dir):
    return scrape(routes, images=True, images_dir=images_dir)


def test_saved_with_original_bytes_and_records(tmp_path):
    png = "https://cdn.test/a/photo.PNG?v=3"
    webp = "https://cdn.test/b/noext"
    routes = {
        **shop([png, webp]),
        png: (200, PNG, {"Content-Type": "image/png"}),
        webp: (200, WEBP, {"Content-Type": "image/webp"}),
    }
    items, result, _ = run_images(routes, tmp_path)
    records = items[0]["scraped_images"]
    assert records == [
        {"url": png, "path": f"full/{sha1(png)}.png", "checksum": md5(PNG), "status": "downloaded"},
        {
            "url": webp,
            "path": f"full/{sha1(webp)}.webp",
            "checksum": md5(WEBP),
            "status": "downloaded",
        },
    ]
    assert (tmp_path / records[0]["path"]).read_bytes() == PNG
    assert (tmp_path / records[1]["path"]).read_bytes() == WEBP
    assert result.stores[0].images_failed == 0


def test_same_url_downloaded_once_per_store(tmp_path):
    url = "https://cdn.test/shared.jpg"
    routes = {**shop([url], [url], [url]), url: (200, JPEG, {"Content-Type": "image/jpeg"})}
    items, _, fetcher = run_images(routes, tmp_path)
    assert fetcher.count(url) == 1
    assert len(items) == 3
    for item in items:
        assert [r["url"] for r in item["scraped_images"]] == [url]
        assert item["scraped_images"][0]["checksum"] == md5(JPEG)


def test_fresh_file_reused_without_request(tmp_path):
    url = "https://cdn.test/cached.jpg"
    full = tmp_path / "full"
    full.mkdir()
    (full / f"{sha1(url)}.jpg").write_bytes(b"old-bytes")
    items, _, fetcher = run_images({**shop([url]), url: (200, JPEG)}, tmp_path)
    assert fetcher.count(url) == 0
    assert items[0]["scraped_images"] == [
        {
            "url": url,
            "path": f"full/{sha1(url)}.jpg",
            "checksum": md5(b"old-bytes"),
            "status": "uptodate",
        }
    ]


def test_old_file_redownloaded(tmp_path):
    url = "https://cdn.test/stale.jpg"
    full = tmp_path / "full"
    full.mkdir()
    path = full / f"{sha1(url)}.jpg"
    path.write_bytes(b"old-bytes")
    old = time.time() - 100 * 86400
    os.utime(path, (old, old))
    items, _, fetcher = run_images({**shop([url]), url: (200, JPEG)}, tmp_path)
    assert fetcher.count(url) == 1
    record = items[0]["scraped_images"][0]
    assert record["status"] == "downloaded"
    assert record["checksum"] == md5(JPEG)
    assert path.read_bytes() == JPEG


def test_failed_image_omitted_and_counted(tmp_path):
    good = "https://cdn.test/good.jpg"
    bad = "https://cdn.test/bad.jpg"
    routes = {**shop([good, bad]), good: (200, JPEG), bad: (404, "")}
    items, result, _ = run_images(routes, tmp_path)
    assert [r["url"] for r in items[0]["scraped_images"]] == [good]
    assert result.stores[0].images_failed == 1
    assert result.stores[0].status == "ok"
    assert not (tmp_path / "full" / f"{sha1(bad)}.jpg").exists()


def test_image_redirect_followed(tmp_path):
    url = "https://cdn.test/old-location.jpg"
    final = "https://img.test/new-location.jpg"
    routes = {**shop([url]), url: redirect(final, 302), final: (200, JPEG)}
    items, result, _ = run_images(routes, tmp_path)
    record = items[0]["scraped_images"][0]
    assert record["url"] == url
    assert record["path"] == f"full/{sha1(url)}.jpg"
    assert (tmp_path / record["path"]).read_bytes() == JPEG
    assert result.stores[0].images_failed == 0


def test_no_files_without_images(tmp_path):
    url = "https://cdn.test/x.jpg"
    items, _, fetcher = scrape({**shop([url]), url: (200, JPEG)}, images_dir=tmp_path)
    assert fetcher.count(url) == 0
    assert "scraped_images" not in items[0]
    assert list(tmp_path.iterdir()) == []


IMAGE_BODIES = {
    "jpeg": (JPEG, ".jpg"),
    "png": (PNG, ".png"),
    "gif87a": (b"GIF87a\x01\x00", ".gif"),
    "gif89a": (b"GIF89a\x01\x00", ".gif"),
    "webp": (WEBP, ".webp"),
    "avif": (ftyp(b"avif", b"avif", b"mif1", b"miaf"), ".avif"),
    "avif sequence": (ftyp(b"avis", b"avis", b"msf1"), ".avif"),
    "heic": (ftyp(b"heic", b"mif1", b"heic"), ".heic"),
    "heic under generic brand": (ftyp(b"mif1", b"mif1", b"heic"), ".heic"),
    "heif": (ftyp(b"mif1", b"mif1"), ".heif"),
    "bmp": (b"BM\x36\x00\x00\x00", ".bmp"),
    "tiff little-endian": (b"II*\x00\x08\x00", ".tiff"),
    "tiff big-endian": (b"MM\x00*\x00\x08", ".tiff"),
    "ico": (b"\x00\x00\x01\x00\x01\x00", ".ico"),
    "jpeg xl codestream": (b"\xff\x0a\xfa\x7f", ".jxl"),
    "jpeg xl container": (b"\x00\x00\x00\x0cJXL \r\n\x87\n\x00\x00\x00\x14ftypjxl ", ".jxl"),
}
NOT_IMAGES = {
    "html": b"<!DOCTYPE html><html><body>Not Found</body></html>",
    "json": b'{"errors": "Not Found"}',
    "xml error": b'<?xml version="1.0"?><Error><Code>AccessDenied</Code></Error>',
    "svg": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    "plain text": b"Access Denied",
    "pdf": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3",
    "zip": b"PK\x03\x04\x14\x00",
    "gzip": b"\x1f\x8b\x08\x00",
    "windows exe": b"MZ\x90\x00\x03\x00",
    "mp4": ftyp(b"isom", b"isom", b"iso2", b"avc1", b"mp41"),
    "ftyp size past its brands": b"\xff\xff\xff\xffftypisom\x00\x00\x00\x00",
    "too short": b"\xff\xd8",
}


@pytest.mark.parametrize("body,ext", IMAGE_BODIES.values(), ids=IMAGE_BODIES.keys())
def test_format_read_from_leading_bytes(body, ext):
    assert image_extension(body) == ext


@pytest.mark.parametrize("body", NOT_IMAGES.values(), ids=NOT_IMAGES.keys())
def test_non_images_have_no_format(body):
    assert image_extension(body) is None


def test_saved_under_the_format_it_is_not_the_url_or_header(tmp_path):
    url = "https://cdn.test/a/really-a-jpeg.png"
    routes = {**shop([url]), url: (200, JPEG, {"Content-Type": "application/x-msdownload"})}
    items, result, _ = run_images(routes, tmp_path)
    assert items[0]["scraped_images"][0]["path"] == f"full/{sha1(url)}.jpg"
    assert result.stores[0].images_failed == 0


@pytest.mark.parametrize("body", NOT_IMAGES.values(), ids=NOT_IMAGES.keys())
def test_non_image_body_fails_and_isnt_saved(tmp_path, body):
    url = "https://cdn.test/photo.jpg"
    items, result, _ = run_images({**shop([url]), url: (200, body)}, tmp_path)
    assert items[0]["scraped_images"] == []
    assert result.stores[0].images_failed == 1
    assert not (tmp_path / "full").exists()


def test_cached_jpeg_reused_for_a_jpeg_url(tmp_path):
    url = "https://cdn.test/photo.JPEG"
    full = tmp_path / "full"
    full.mkdir()
    (full / f"{sha1(url)}.jpg").write_bytes(JPEG)
    items, _, fetcher = run_images({**shop([url]), url: (200, JPEG)}, tmp_path)
    assert fetcher.count(url) == 0
    assert items[0]["scraped_images"][0]["status"] == "uptodate"
