"""Regressions found by the post-implementation reviews."""

import asyncio
import csv
import logging
import os
import signal
import sys
import time

import httpx
import pytest
import respx

import shopify_spy as spy
from shopify_spy._config import Config
from shopify_spy._export import write_items
from shopify_spy._fetch import FakeFetcher, HttpxFetcher, Identity, Request
from tests.flow_support import FAST, STORE, bulk_url, products, scrape, urlset, woo_url


def test_items_survive_image_save_failure(tmp_path):
    images_dir = tmp_path / "not-a-dir"
    images_dir.write_text("a file, so saving images fails")
    routes = {bulk_url(1): products(3)} | {
        f"https://cdn.test/{i}.jpg": (200, b"\xff\xd8\xff\xe0jpeg") for i in range(3)
    }
    items, result, _ = scrape(routes, images=True, images_dir=images_dir)
    assert len(items) == 3
    assert all(item["scraped_images"] == [] for item in items)
    assert result.stores[0].items == 3
    assert result.stores[0].images_failed == 3


def test_limit_delivers_exactly_what_it_counts_with_a_slow_consumer():
    # Concurrent sitemap fetches race to the limit while the queue is full
    locs = [f"{STORE}/products/p{i}" for i in range(200)]
    routes = {f"{STORE}/sitemap.xml": urlset(*locs)}
    routes |= {f"{loc}.json": {"product": {"id": i}} for i, loc in enumerate(locs)}
    run = spy.scrape(
        STORE, fetcher_factory=lambda: FakeFetcher(routes), max_items_per_store=101, **FAST
    )
    delivered = 0
    for _ in run:
        if delivered == 0:
            time.sleep(0.3)  # let every sitemap fetch pile up behind the full queue
        delivered += 1
    assert delivered == run.result.stores[0].items == 101


async def test_iterating_after_aclose_ends():
    routes = {bulk_url(1): products(250), bulk_url(2): products(250, 250), bulk_url(3): products(1)}
    run = spy.scrape_async(STORE, fetcher_factory=lambda: FakeFetcher(routes), **FAST)
    iterator = run.__aiter__()
    await iterator.__anext__()
    await run.aclose()
    async with asyncio.timeout(2):
        try:
            await iterator.__anext__()
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("items delivered after aclose()")
    assert run.result.stores[0].finish_reason == "closed"


async def test_aclose_while_cancelled_still_finishes_result():
    async def hang(request):
        await asyncio.sleep(30)

    run = spy.scrape_async(STORE, fetcher_factory=lambda: FakeFetcher({bulk_url(1): hang}), **FAST)

    async def consume():
        async with run:
            async for _ in run:
                pass

    task = asyncio.ensure_future(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert run.result.stores[0].finish_reason == "closed"


@respx.mock
async def test_httpx_fetcher_never_stores_cookies():
    respx.get("https://a.test/1").mock(
        return_value=httpx.Response(200, headers={"Set-Cookie": "sid=abc; Path=/"})
    )
    second = respx.get("https://a.test/2").mock(return_value=httpx.Response(200))
    async with HttpxFetcher(trust_env=False) as fetcher:
        for path in ("/1", "/2"):
            await fetcher.fetch(Request("GET", f"https://a.test{path}", Identity("ua"), 5))
    assert "cookie" not in second.calls.last.request.headers


def test_woocommerce_failure_after_items_is_partial():
    routes = {woo_url(1): [{"id": i} for i in range(100)], woo_url(2): (500, "")}
    items, result, _ = scrape(routes, platform="woocommerce")
    store = result.stores[0]
    assert len(items) == 100
    assert store.status == spy.StoreStatus.partial
    assert not store.complete
    assert not store.failed  # partial is only a failure with strict=True
    assert result.failures(strict=True) == [store]


def test_shopify_bulk_failure_without_sitemap_is_partial():
    routes = {bulk_url(1): products(250), bulk_url(2): (500, "")}
    items, result, _ = scrape(routes)
    assert len(items) == 250
    assert result.stores[0].status == spy.StoreStatus.partial


def test_shopify_bulk_failure_recovered_by_sitemap_is_ok():
    locs = [f"{STORE}/products/p{i}" for i in range(252)]
    routes = {
        bulk_url(1): products(250),
        bulk_url(2): (500, ""),
        f"{STORE}/sitemap.xml": urlset(*locs),
    }
    routes |= {f"{STORE}/products/p{i}.json": {"product": {"id": i}} for i in (250, 251)}
    items, result, _ = scrape(routes)
    assert len(items) == 252
    assert result.stores[0].status == spy.StoreStatus.ok


def test_library_logger_has_null_handler():
    handlers = logging.getLogger("shopify_spy").handlers
    assert any(isinstance(h, logging.NullHandler) for h in handlers)


def test_csv_append_reuses_existing_header(tmp_path):
    path = tmp_path / "out.csv"
    write_items([{"a": 1, "b": 2}], path)
    write_items([{"b": 3, "a": 4, "c": 5}], path, append=True)
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    assert rows == [{"a": "1", "b": "2"}, {"a": "4", "b": "3"}]


def test_option_table_matches_config_and_library_defaults():
    # Plan §4.3: every config-mapped option is a library option with the same default
    library = spy.ScrapeOptions()
    kwargs = Config().to_kwargs()
    for name, value in kwargs.items():
        assert name in spy.ScrapeOptions.model_fields, name
        if name == "images_dir":
            continue  # derived from output.dir, and only when images are on
        assert getattr(library, name) == value, name


@pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT to the test process")
def test_cli_second_ctrl_c_forces_stop_and_keeps_result():
    from shopify_spy import cli

    async def hang(request):
        await asyncio.sleep(3600)

    fetcher = FakeFetcher({bulk_url(1): hang})
    kwargs = {"fetcher_factory": lambda: fetcher, "throttle": False, "on_failure": "ignore",
              "bail": None}  # fmt: skip

    async def main():
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, os.kill, os.getpid(), signal.SIGINT)  # graceful
        loop.call_later(0.4, os.kill, os.getpid(), signal.SIGINT)  # forced
        return await cli._scrape([STORE], kwargs, writer=None, peek=True, show_counter=False)

    result, forced = asyncio.run(main())
    assert forced
    assert result.stores[0].finish_reason == "shutdown"
