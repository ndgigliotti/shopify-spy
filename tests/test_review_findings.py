"""Regressions for the /code-review findings on the httpx rewrite."""

import asyncio
import gc
import hashlib
import os
import time
import warnings

import shopify_spy as spy
from shopify_spy._fetch import FakeFetcher, Seq
from shopify_spy._images import ImageDownloader
from shopify_spy._sitemap import parse
from tests.flow_support import (
    FAST,
    STORE,
    bulk_url,
    product,
    products,
    scrape,
    sitemapindex,
    urlset,
    woo_url,
)

IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"


def product_urlset_with_images(*handles: str) -> tuple[int, str, dict[str, str]]:
    entries = "".join(
        f"<url><loc>{STORE}/products/{h}</loc>"
        f"<image:image><image:loc>https://cdn.test/s/files/products/{h}.jpg?v=1</image:loc>"
        "</image:image></url>"
        for h in handles
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:image="{IMAGE_NS}">'
        f"{entries}</urlset>"
    )
    return 200, body, {"Content-Type": "application/xml"}


def test_image_locs_are_not_page_urls():
    _, body, _ = product_urlset_with_images("a", "b")
    assert parse(body.encode()) == ("urlset", [f"{STORE}/products/a", f"{STORE}/products/b"])


def test_sitemap_fallback_does_not_fetch_image_urls():
    routes = {
        f"{STORE}/sitemap.xml": product_urlset_with_images("a"),
        f"{STORE}/products/a.json": {"product": {"id": 1}},
    }
    items, _, fetcher = scrape(routes)
    assert len(items) == 1
    assert not any("cdn.test" in r.url for r in fetcher.requests)


def test_child_sitemaps_without_wanted_entries_are_not_fetched():
    routes = {
        bulk_url(1): products(3),
        f"{STORE}/sitemap.xml": sitemapindex(
            f"{STORE}/sitemap_products_1.xml",
            f"{STORE}/sitemap_pages_1.xml",
            f"{STORE}/sitemap_collections_1.xml",
        ),
        f"{STORE}/sitemap_collections_1.xml": urlset(f"{STORE}/collections/c"),
        f"{STORE}/collections/c.json": {"collection": {"id": 9}},
    }
    items, _, fetcher = scrape(routes, collections=True)
    assert len(items) == 4
    fetched = {r.url for r in fetcher.requests}
    assert f"{STORE}/sitemap_products_1.xml" not in fetched  # bulk already covered them
    assert f"{STORE}/sitemap_pages_1.xml" not in fetched


def test_first_item_at_the_bail_deadline_is_never_lost():
    # The first item can arrive while the bail timer is expiring; it must either
    # be delivered (and counted) or the store must bail with nothing counted.
    async def slow_product(request):
        await asyncio.sleep(0.2)
        return 200, '{"product": {"id": 1}}'

    routes = {
        f"{STORE}/sitemap.xml": urlset(f"{STORE}/products/a"),
        f"{STORE}/products/a.json": slow_product,
    }
    for _ in range(15):
        items, result, _ = scrape(routes, bail=0.2)
        store = result.stores[0]
        assert store.status != spy.StoreStatus.error, store.errors
        assert len(items) == store.items


def test_stores_stopped_before_starting_are_not_failures():
    routes = {bulk_url(1, s): products(3) for s in ("https://a.test", "https://b.test")}
    run = spy.scrape(
        ["https://a.test", "https://b.test", "https://c.test"],
        fetcher_factory=lambda: FakeFetcher(routes),
        max_concurrent_stores=1,
        **FAST,
    )
    run.first()
    unstarted = run.result.stores[1:]
    assert [s.status for s in unstarted] == [spy.StoreStatus.stopped] * 2
    assert not any(s.failed for s in unstarted)


def test_malformed_sitemap_url_does_not_end_the_store():
    routes = {
        f"{STORE}/sitemap.xml": urlset(f"{STORE}/products/bad\x7fname", f"{STORE}/products/ok"),
        f"{STORE}/products/ok.json": {"product": {"id": 1}},
    }
    items, result, _ = scrape(routes)
    assert len(items) == 1
    assert result.stores[0].status == spy.StoreStatus.ok


async def test_bare_break_from_async_for_stops_the_run():
    routes = {bulk_url(1): products(250), bulk_url(2): products(250, 250), bulk_url(3): products(1)}
    run = spy.scrape_async(STORE, fetcher_factory=lambda: FakeFetcher(routes), **FAST)
    async for _ in run:
        break
    gc.collect()  # drop the iterator; the event loop's finalizer closes it
    async with asyncio.timeout(2):
        while not run.done:
            await asyncio.sleep(0.01)
    assert run.result.stores[0].finish_reason == "closed"


def test_scrapy_era_jpeg_is_not_reused_for_a_png_url(tmp_path):
    png_url = "https://cdn.test/a.png"
    full = tmp_path / "full"
    full.mkdir()
    old = full / f"{hashlib.sha1(png_url.encode()).hexdigest()}.jpg"  # Scrapy's re-encode
    old.write_bytes(b"lossy jpeg")
    routes = {
        bulk_url(1): {"products": [product(1, images=[{"src": png_url}])]},
        png_url: (200, b"original png", {"Content-Type": "image/png"}),
    }
    items, _, _ = scrape(routes, images=True, images_dir=tmp_path)
    record = items[0]["scraped_images"][0]
    assert record["status"] == "downloaded"
    assert record["path"].endswith(".png")


def test_matching_cached_image_is_reused(tmp_path):
    url = "https://cdn.test/a.png"
    full = tmp_path / "full"
    full.mkdir()
    (full / f"{hashlib.sha1(url.encode()).hexdigest()}.png").write_bytes(b"cached")
    routes = {bulk_url(1): {"products": [product(1, images=[{"src": url}])]}}
    items, _, fetcher = scrape(routes, images=True, images_dir=tmp_path)
    assert items[0]["scraped_images"][0]["status"] == "uptodate"
    assert fetcher.count(url) == 0


def test_unexpected_image_errors_are_counted(tmp_path, monkeypatch):
    async def broken(self, url):
        raise ValueError("boom")

    monkeypatch.setattr(ImageDownloader, "_download", broken)
    items, result, _ = scrape({bulk_url(1): products(1)}, images=True, images_dir=tmp_path)
    assert items[0]["scraped_images"] == []
    assert result.stores[0].images_failed == 1


def test_bulk_paging_stops_when_pages_repeat():
    # A server that ignores ?page= returns page 1 forever
    def same_page(request):
        return products(250)

    fetcher = FakeFetcher({bulk_url(i): same_page for i in range(1, 5)})
    items, result, _ = scrape(fetcher)
    assert len(items) == 250
    assert result.stores[0].status == spy.StoreStatus.ok


def test_woocommerce_paging_stops_when_pages_repeat():
    page = [{"id": i} for i in range(100)]
    routes = {woo_url(i): page for i in range(1, 5)}
    items, _, _ = scrape(routes, platform="woocommerce")
    assert len(items) == 100


def test_woocommerce_null_images_with_images_enabled(tmp_path):
    routes = {woo_url(1): [{"id": 1, "images": None}], woo_url(2): []}
    items, result, _ = scrape(routes, platform="woocommerce", images=True, images_dir=tmp_path)
    assert items[0]["image_urls"] == []
    assert result.stores[0].status == spy.StoreStatus.ok


def test_failure_warning_points_at_the_callers_line():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        list(
            spy.scrape(
                STORE,
                fetcher_factory=lambda: FakeFetcher({}),
                **{**FAST, "on_failure": "warn"},
            )
        )
    [warning] = [w for w in caught if issubclass(w.category, spy.ScrapeWarning)]
    assert os.path.basename(warning.filename) == os.path.basename(__file__)


async def test_async_failure_warning_points_at_the_callers_line():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await spy.scrape_async(
            STORE, fetcher_factory=lambda: FakeFetcher({}), **{**FAST, "on_failure": "warn"}
        )
    [warning] = [w for w in caught if issubclass(w.category, spy.ScrapeWarning)]
    assert os.path.basename(warning.filename) == os.path.basename(__file__)


def test_retry_waits_survive_zero_throttle_max_delay():
    # The retry cap no longer follows throttle_max_delay
    fetcher = FakeFetcher({bulk_url(1): Seq([(503, ""), products(1)])})
    start = time.monotonic()
    items, _, _ = scrape(fetcher, throttle_max_delay=0, _backoff=True)
    assert len(items) == 1
    assert time.monotonic() - start >= 0.2  # a real backoff wait happened
