"""Image downloads (plan §3.6 as amended by D1/D13, §4.9)."""

import hashlib
import os
import time

from tests.flow_support import bulk_url, redirect, scrape

PNG = b"\x89PNG\r\n\x1a\nfake-png-bytes"
WEBP = b"RIFF\x00\x00\x00\x00WEBPfake"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


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
