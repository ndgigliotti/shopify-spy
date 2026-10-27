"""The sync API's paused-loop clock (plan §4.4): consumer pauses don't count as time."""

import asyncio
import time

import shopify_spy as spy
from shopify_spy.api import _PausedClockLoop
from shopify_spy.fetch import FakeFetcher, FetchTimeout
from tests.flow_support import STORE, bulk_url, products, scrape

SLOW = "https://slow.test"
PAUSE = 0.6


def _consume_slowly(routes, **options):
    fetcher = FakeFetcher(routes)
    run = spy.scrape(
        [STORE, SLOW],
        fetcher_factory=lambda: fetcher,
        throttle=False,
        _backoff=False,
        on_failure="ignore",
        **options,
    )
    items = []
    for item in run:
        items.append(item)
        time.sleep(PAUSE)
    return items, run.result


def test_consumer_pauses_do_not_cause_bail():
    async def slow(request):
        await asyncio.sleep(0.2)
        return products(1)

    routes = {bulk_url(1): products(2), bulk_url(1, SLOW): slow}
    items, result = _consume_slowly(routes, bail=0.5)
    fast_store, slow_store = result.stores
    assert fast_store.status == "ok"
    assert slow_store.finish_reason == "finished"
    assert slow_store.status == "ok"
    assert len(items) == 3


def test_consumer_pauses_do_not_cause_request_timeouts():
    timeouts = []

    async def slow(request):
        # Enforce request.timeout like HttpxFetcher does
        try:
            async with asyncio.timeout(request.timeout):
                await asyncio.sleep(0.2)
        except TimeoutError:
            timeouts.append(request.url)
            raise FetchTimeout("timed out") from None
        return products(1)

    routes = {bulk_url(1): products(2), bulk_url(1, SLOW): slow}
    items, result = _consume_slowly(routes, bail=None, request_timeout=0.5, retries=0)
    assert timeouts == []
    assert result.stores[1].status == "ok"
    assert len(items) == 3


def test_fast_run_unaffected():
    items, result, _ = scrape({bulk_url(1): products(2)}, bail=0.5)
    assert len(items) == 2
    assert result.stores[0].status == "ok"


def test_paused_clock_loop_excludes_pauses():
    loop = _PausedClockLoop()
    try:

        async def now():
            return loop.time()

        t1 = loop.run_until_complete(now())
        time.sleep(0.3)
        t2 = loop.run_until_complete(now())
        assert t2 - t1 < 0.1
        assert t2 >= t1
        # Between runs, time() doesn't advance either
        t3 = loop.time()
        time.sleep(0.2)
        t4 = loop.run_until_complete(now())
        assert t4 - t3 < 0.1
    finally:
        loop.close()


def test_paused_clock_loop_advances_while_running():
    loop = _PausedClockLoop()
    try:

        async def measure():
            start = loop.time()
            await asyncio.sleep(0.2)
            return loop.time() - start

        assert loop.run_until_complete(measure()) >= 0.19
    finally:
        loop.close()
