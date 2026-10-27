"""Public API behavior: spy.scrape / spy.scrape_async (plan §4.3, §4.4, §4.8, §7.1)."""

import asyncio
import gc
import subprocess
import sys
import time
import warnings

import pytest

import shopify_spy as spy
from shopify_spy.fetch import FakeFetcher, FetchConnectionError, FetchTLSError
from tests.flow_support import FAST, STORE, bulk_url, products, scrape, urlset

OTHER = "https://other.test"


def fake(routes, **kw):
    fetcher = FakeFetcher(routes, **kw)
    return lambda: fetcher


def ok_routes(store=STORE, n=2):
    return {bulk_url(1, store): products(n)}


def hang(seconds=30):
    async def respond(request):
        await asyncio.sleep(seconds)
        return products(1)

    return respond


# --- URL handling ---


def test_single_string_url():
    items, result, _ = scrape(ok_routes(), "shop.test")
    assert len(items) == 2
    assert result.stores[0].input_url == "shop.test"
    assert result.stores[0].url == STORE


@pytest.mark.parametrize("urls", ["a.com,b.com", "a.com b.com", "a.com\nb.com"])
def test_multi_url_string_rejected(urls):
    with pytest.raises(ValueError, match="list"):
        spy.scrape(urls)


@pytest.mark.parametrize(
    "url", ["ftp://a.com", "http://", "https://a.com:99999", "https://user@a.com"]
)
def test_invalid_url_raises_at_call_time(url):
    with pytest.raises(ValueError, match="invalid URL"):
        spy.scrape([url])


def test_duplicates_deduped_keeping_first_input_url():
    routes = ok_routes()
    urls = ["shop.test/products/x", STORE, "https://SHOP.test/collections/y"]
    items, result, fetcher = scrape(routes, urls)
    assert len(result.stores) == 1
    assert result.stores[0].input_url == "shop.test/products/x"
    assert len(items) == 2
    assert fetcher.count(bulk_url(1)) == 1


def test_paths_ignored():
    _, result, fetcher = scrape(ok_routes(), f"{STORE}/products/some-hat?x=1")
    assert result.stores[0].url == STORE
    assert fetcher.count(bulk_url(1)) == 1


def test_platform_auto_rejected():
    with pytest.raises(ValueError, match="auto-detection isn't supported yet"):
        spy.scrape(STORE, platform="auto")


def test_images_without_images_dir():
    with pytest.raises(ValueError, match="images_dir"):
        spy.scrape(STORE, images=True)


def test_unknown_kwarg_rejected():
    with pytest.raises(ValueError, match="limit"):
        spy.scrape(STORE, limit=5)


def test_nothing_runs_until_iteration():
    fetcher = FakeFetcher(ok_routes())
    spy.scrape(STORE, fetcher_factory=lambda: fetcher, **FAST)
    assert fetcher.requests == []


# --- on_failure and strict ---


def test_on_failure_raise_when_all_failed():
    run = spy.scrape(["a.test", "b.test"], fetcher_factory=fake({}), throttle=False)
    with pytest.raises(spy.ScrapeFailed) as exc_info:
        list(run)
    message = str(exc_info.value)
    assert "a.test: not_found" in message
    assert "b.test: not_found" in message
    assert len(exc_info.value.result.stores) == 2
    assert run.result.failed == exc_info.value.result.failed


def test_on_failure_raise_not_when_some_succeed():
    run = spy.scrape([STORE, OTHER], fetcher_factory=fake(ok_routes()), throttle=False)
    assert len(list(run)) == 2
    assert [s.status for s in run.result.stores] == ["ok", "not_found"]


def test_strict_raises_when_any_failed_and_yielded_items_stay_yielded():
    run = spy.scrape([STORE, OTHER], fetcher_factory=fake(ok_routes()), throttle=False, strict=True)
    seen = []
    with pytest.raises(spy.ScrapeFailed, match="other.test: not_found"):
        for item in run:
            seen.append(item)
    assert len(seen) == 2


def test_on_failure_warn():
    run = spy.scrape("a.test", fetcher_factory=fake({}), throttle=False, on_failure="warn")
    with pytest.warns(spy.ScrapeWarning, match="a.test: not_found"):
        assert list(run) == []
    assert run.result.failed[0].status == "not_found"


def test_on_failure_ignore():
    run = spy.scrape("a.test", fetcher_factory=fake({}), throttle=False, on_failure="ignore")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert list(run) == []
    assert run.result.failed[0].status == "not_found"


# --- Results and lifecycle ---


def test_result_before_completion_raises():
    run = spy.scrape(STORE, fetcher_factory=fake(ok_routes()), **FAST)
    with pytest.raises(RuntimeError, match="iterate to completion"):
        run.result
    it = iter(run)
    next(it)
    with pytest.raises(RuntimeError, match="iterate to completion"):
        run.result
    run.close()


def test_result_after_break_marks_unfinished_closed():
    routes = {bulk_url(1): products(250), bulk_url(2): hang(), bulk_url(1, OTHER): hang()}
    run = spy.scrape([STORE, OTHER], fetcher_factory=fake(routes), **FAST)
    with run:
        for _ in run:
            break
    result = run.result
    assert [s.finish_reason for s in result.stores] == ["closed", "closed"]
    assert not any(s.complete for s in result.stores)
    assert result.stores[0].items >= 1


def test_result_after_bare_break_without_with():
    # Plan §4.3/§4.4: `break` closes the run and run.result is valid afterwards
    routes = {bulk_url(1): products(250), bulk_url(2): hang()}
    run = spy.scrape(STORE, fetcher_factory=fake(routes), **FAST)
    for _ in run:
        break
    assert run.result.stores[0].finish_reason == "closed"


def test_collect_returns_items():
    result = spy.scrape(
        [STORE, OTHER], fetcher_factory=fake({**ok_routes(), **ok_routes(OTHER, 3)}), **FAST
    ).collect()
    assert len(result.items) == 5
    assert result.total_items == 5
    assert result.finish_reason == "finished"
    assert {s.status for s in result.stores} == {"ok"}


def test_first_returns_one_item_and_closes():
    routes = {bulk_url(1): products(250), bulk_url(2): hang()}
    run = spy.scrape(STORE, fetcher_factory=fake(routes), **FAST)
    start = time.monotonic()
    item = run.first()
    assert item["product"]["handle"] == "p0"
    assert time.monotonic() - start < 3
    assert run.result.stores[0].finish_reason == "closed"


def test_first_raises_scrape_failed_without_items():
    run = spy.scrape(STORE, fetcher_factory=fake({}), **FAST)
    with pytest.raises(spy.ScrapeFailed, match="not_found"):
        run.first()


def test_iterating_twice_raises():
    run = spy.scrape(STORE, fetcher_factory=fake(ok_routes()), **FAST)
    list(run)
    with pytest.raises(RuntimeError, match="once"):
        iter(run)


def test_stores_snapshot():
    run = spy.scrape(STORE, fetcher_factory=fake(ok_routes()), **FAST)
    assert run.stores == []
    assert not run.done
    list(run)
    assert run.done
    assert run.stores[0].items == 2


def test_zip_interleaved_sync_runs():
    a = spy.scrape(STORE, fetcher_factory=fake(ok_routes(n=3)), **FAST)
    b = spy.scrape(OTHER, fetcher_factory=fake(ok_routes(OTHER, 3)), **FAST)
    pairs = list(zip(a, b))
    assert len(pairs) == 3
    assert {x["store"] for x, _ in pairs} == {"shop.test"}
    assert {y["store"] for _, y in pairs} == {"other.test"}


class ConsumerError(Exception):
    pass


def test_exception_in_consumer_with_block_cleans_up():
    routes = {bulk_url(1): products(250), bulk_url(2): hang()}
    start = time.monotonic()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run = spy.scrape(STORE, fetcher_factory=fake(routes), **FAST)
        with pytest.raises(ConsumerError):
            with run:
                for _ in run:
                    raise ConsumerError
        gc.collect()
    assert time.monotonic() - start < 3
    assert run.result.stores[0].finish_reason == "closed"
    assert [str(w.message) for w in caught] == []


def test_exception_in_bare_for_loop_cleans_up():
    routes = {bulk_url(1): products(250), bulk_url(2): hang()}
    start = time.monotonic()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        run = spy.scrape(STORE, fetcher_factory=fake(routes), **FAST)

        def consume():
            for _ in run:
                raise ConsumerError

        with pytest.raises(ConsumerError):
            consume()
        gc.collect()
    assert time.monotonic() - start < 3
    # Leaving the for loop by an exception closes the run (plan §4.4)
    assert run.done
    assert run.result.stores[0].finish_reason == "closed"
    assert [str(w.message) for w in caught] == []


def test_store_error_isolated():
    def explode(request):
        raise RuntimeError("adapter bug")

    routes = {**ok_routes(), bulk_url(1, OTHER): explode}
    items, result, _ = scrape(routes, [STORE, OTHER])
    assert len(items) == 2
    good, bad = result.stores
    assert good.status == "ok"
    assert bad.status == spy.StoreStatus.error
    assert bad.finish_reason == "error"
    assert any("adapter bug" in e for e in bad.errors)
    assert result.finish_reason == "crawler_failed"


def test_request_shutdown_ends_iteration_normally():
    routes = {bulk_url(1): products(250), bulk_url(2): products(250, 250)}
    run = spy.scrape(STORE, fetcher_factory=fake(routes), **FAST)
    start = time.monotonic()
    count = 0
    for _ in run:
        count += 1
        if count == 1:
            run.request_shutdown()
    elapsed = time.monotonic() - start
    store = run.result.stores[0]
    assert store.finish_reason == "shutdown"
    assert not store.complete
    assert 1 <= count < 500
    assert elapsed < 5, f"graceful shutdown took {elapsed:.1f}s"


def test_nothing_printed_to_stdout(capsys):
    scrape(ok_routes(), [STORE, OTHER])
    assert capsys.readouterr().out == ""


def test_import_boundary():
    code = (
        "import shopify_spy, sys; print([m for m in ('typer','yaml','scrapy') if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# --- Status diagnosis ---


def _status(fetcher, **options):
    _, result, _ = scrape(fetcher, **options)
    return result.stores[0]


def test_status_robots_blocked():
    store = _status(FakeFetcher({f"{STORE}/robots.txt": (200, "User-agent: *\nDisallow: /\n")}))
    assert store.status == "robots_blocked"
    assert store.robots_disallowed > 0


def test_status_forbidden_after_retries():
    fetcher = FakeFetcher({f"{STORE}/robots.txt": (404, "")}, default_status=403)
    store = _status(fetcher, retries=2)
    assert store.status == "forbidden"
    assert fetcher.count(bulk_url(1)) == 3
    # UA rotated after each 403
    agents = [r.identity.user_agent for r in fetcher.requests if r.url == bulk_url(1)]
    assert len(set(agents)) > 1


def test_status_not_found():
    assert _status(FakeFetcher({})).status == "not_found"


def test_status_timed_out():
    start = time.monotonic()
    store = _status(FakeFetcher({bulk_url(1): hang(5)}), bail=0.3)
    assert time.monotonic() - start < 3
    assert store.status == "timed_out"
    assert store.finish_reason == "bail"


def test_status_no_response():
    err = FetchConnectionError("connection refused")
    store = _status(FakeFetcher({bulk_url(1): err, f"{STORE}/sitemap.xml": err}))
    assert store.status == "no_response"


def test_status_tls_error():
    err = FetchTLSError("certificate verify failed")
    fetcher = FakeFetcher({bulk_url(1): err, f"{STORE}/sitemap.xml": err})
    store = _status(fetcher)
    assert store.status == "tls_error"
    assert fetcher.count(bulk_url(1)) == 1  # not retried


def test_status_empty():
    fetcher = FakeFetcher({bulk_url(1): {"products": []}, f"{STORE}/sitemap.xml": urlset()})
    store = _status(fetcher)
    assert store.status == "empty"
    assert not store.failed


# --- Async API ---


async def test_sync_scrape_inside_running_loop_raises():
    with pytest.raises(RuntimeError, match="scrape_async"):
        list(spy.scrape(STORE, fetcher_factory=fake(ok_routes()), **FAST))


async def test_async_for_under_running_loop():
    async with asyncio.timeout(5):
        run = spy.scrape_async(STORE, fetcher_factory=fake(ok_routes()), **FAST)
        items = [item async for item in run]
    assert len(items) == 2
    assert run.result.stores[0].status == "ok"


async def test_await_returns_run_result():
    async with asyncio.timeout(5):
        result = await spy.scrape_async(STORE, fetcher_factory=fake(ok_routes(n=4)), **FAST)
    assert isinstance(result, spy.RunResult)
    assert len(result.items) == 4


async def test_async_collect_and_first():
    async with asyncio.timeout(5):
        result = await spy.scrape_async(STORE, fetcher_factory=fake(ok_routes()), **FAST).collect()
        assert len(result.items) == 2
        item = await spy.scrape_async(STORE, fetcher_factory=fake(ok_routes()), **FAST).first()
    assert item["product"]["handle"] == "p0"


async def test_async_first_raises_without_items():
    async with asyncio.timeout(5):
        with pytest.raises(spy.ScrapeFailed):
            await spy.scrape_async(STORE, fetcher_factory=fake({}), **FAST).first()


async def test_async_on_failure_raise():
    async with asyncio.timeout(5):
        with pytest.raises(spy.ScrapeFailed, match="a.test: not_found"):
            await spy.scrape_async("a.test", fetcher_factory=fake({}), throttle=False)


async def test_async_break_then_aclose():
    routes = {bulk_url(1): products(250), bulk_url(2): hang()}
    async with asyncio.timeout(5):
        run = spy.scrape_async(STORE, fetcher_factory=fake(routes), **FAST)
        async for _ in run:
            break
        await run.aclose()
    assert run.result.stores[0].finish_reason == "closed"


async def test_async_with_closes_on_break():
    routes = {bulk_url(1): products(250), bulk_url(2): hang()}
    async with asyncio.timeout(5):
        async with spy.scrape_async(STORE, fetcher_factory=fake(routes), **FAST) as run:
            async for _ in run:
                break
    assert run.done
    assert run.result.stores[0].finish_reason == "closed"


async def test_async_request_shutdown():
    routes = {bulk_url(1): products(250), bulk_url(2): products(250, 250)}
    async with asyncio.timeout(6):
        run = spy.scrape_async(STORE, fetcher_factory=fake(routes), **FAST)
        start = time.monotonic()
        count = 0
        async for _ in run:
            count += 1
            if count == 1:
                run.request_shutdown()
        elapsed = time.monotonic() - start
    assert run.result.stores[0].finish_reason == "shutdown"
    assert elapsed < 5, f"graceful shutdown took {elapsed:.1f}s"
