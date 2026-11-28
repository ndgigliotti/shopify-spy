"""Public API behavior: spy.scrape / spy.scrape_async (plan §4.3, §4.4, §4.8, §7.1)."""

import asyncio
import gc
import http.server
import ipaddress
import subprocess
import sys
import threading
import time
import urllib.parse
import warnings

import pytest

import shopify_spy as spy
from shopify_spy import _address, _runner
from shopify_spy._fetch import (
    CurlCffiFetcher,
    FakeFetcher,
    FetchConnectionError,
    FetchTLSError,
    Seq,
)
from tests.flow_support import FAST, STORE, bulk_url, products, scrape, urlset, woo_url

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


def test_negative_min_delay_rejected():
    with pytest.raises(ValueError, match="throttle_min_delay"):
        spy.scrape(STORE, throttle_min_delay=-1)


def test_min_delay_defaults_to_one_second():
    from shopify_spy._api import ScrapeOptions

    assert ScrapeOptions().throttle_min_delay == 1.0


def test_negative_image_delay_rejected():
    with pytest.raises(ValueError, match="throttle_image_delay"):
        spy.scrape(STORE, throttle_image_delay=-1)


def test_image_delay_defaults_to_a_quarter_second():
    from shopify_spy._api import ScrapeOptions

    assert ScrapeOptions().throttle_image_delay == 0.25


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


def test_rate_limited_store_fails():
    run = spy.scrape(STORE, fetcher_factory=fake({bulk_url(1): (429, "")}), **FAST)
    with pytest.raises(spy.ScrapeFailed, match="shop.test: rate_limited"):
        run.collect().raise_for_status()
    [store] = run.result.stores
    assert store.status == spy.StoreStatus.rate_limited
    assert store.failed
    assert store.responses_by_status == {429: 3, 404: 1}  # then the sitemap fallback
    assert store.rate_limited_requests == 1


def test_rate_limited_woocommerce_fallback_fails():
    """Store API gone (404), and the product-page fallback answers 429."""
    # The wrong-platform probe 404s too, so only fallback requests get the 429s
    routes = {
        f"{STORE}/robots.txt": (404, ""),
        woo_url(1): (404, ""),
        f"{STORE}/products.json": (404, ""),
    }
    fetcher = FakeFetcher(routes, default_status=429)
    run = spy.scrape(STORE, platform="woocommerce", fetcher_factory=lambda: fetcher, **FAST)
    run.collect()
    [store] = run.result.stores
    assert store.status == spy.StoreStatus.rate_limited
    assert store.responses_by_status == {404: 1}  # fallback requests aren't counted


def test_retry_after_wait_on_a_new_profile_outlasts_bail():
    """A 429 whose Retry-After is longer than --bail: the store waits and recovers."""

    async def slow_products(request):
        # Answers after --bail would have fired had the wait counted against it
        await asyncio.sleep(0.1)
        return products(1)

    routes = {bulk_url(1): Seq([(429, "", {"Retry-After": "1"}), slow_products])}
    items, result, fetcher = scrape(routes, bail=0.3, _backoff=True, retries=1)
    [store] = result.stores
    assert store.status == spy.StoreStatus.ok
    assert len(items) == 1
    first, retry = [r.identity.impersonate for r in fetcher.requests if r.url == bulk_url(1)]
    assert (first, retry) == ("chrome", "safari")


def test_retry_after_waits_are_excused_from_bail_per_request():
    """An IP-limited store waits out every Retry-After, then ends rate_limited."""
    limited = (429, "", {"Retry-After": "1"})
    routes = {f"{STORE}/robots.txt": limited, bulk_url(1): limited, f"{STORE}/sitemap.xml": limited}
    _, result, _ = scrape(routes, bail=0.1, _backoff=True, retries=0)
    [store] = result.stores
    assert store.status == spy.StoreStatus.rate_limited
    assert store.finish_reason != "bail"
    # robots.txt's pause holds /products.json, whose pause holds sitemap.xml
    assert 1.8 <= store.duration < 3


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
        "import shopify_spy, sys; "
        "print([m for m in ('typer','yaml','scrapy','curl_cffi') if m in sys.modules])"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


class _NotFound(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_run_with_a_user_agent_never_imports_curl_cffi(clean_proxy_env):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _NotFound)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    store = f"http://127.0.0.1:{httpd.server_address[1]}"
    code = (
        "import sys, shopify_spy as spy; "
        f"run = spy.scrape({store!r}, user_agent='test-agent/1.0', on_failure='ignore', "
        "throttle=False); "
        "list(run); "
        "print(404 in run.result.stores[0].responses_by_status, 'curl_cffi' in sys.modules)"
    )
    try:
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
        )
    finally:
        httpd.shutdown()
        httpd.server_close()
    assert out.stdout.strip() == "True False"


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
    # One switch to the next profile, then the block is taken as not about identity
    profiles = [r.identity.impersonate for r in fetcher.requests if r.url == bulk_url(1)]
    assert profiles == ["chrome", "safari", "safari"]


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


# --- Proxy ---


@pytest.mark.parametrize("proxy", ["ftp://h:1", "h:8080", "http://", "http://h:notaport"])
def test_invalid_proxy_raises_at_call_time(proxy):
    with pytest.raises(ValueError, match="proxy must be"):
        spy.scrape("a.com", proxy=proxy)


def test_non_string_proxy_raises_type_error():
    with pytest.raises(TypeError, match="proxy must be a URL string, got int"):
        spy.scrape("a.com", proxy=8080)


def test_default_proxy_accepted(clean_proxy_env):
    spy.scrape("a.com", proxy=None)


def test_invalid_proxy_error_is_redacted():
    with pytest.raises(ValueError) as info:
        spy.scrape("a.com", proxy="ftp://u:secret@h:1")
    assert "secret" not in str(info.value)


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "ALL_PROXY"])
def test_bad_environment_proxy_raises_at_call_time(clean_proxy_env, name):
    clean_proxy_env.setenv(name, "socks4://u:secret@h:1")
    with pytest.raises(ValueError, match="environment") as info:
        spy.scrape("a.com")
    assert "secret" not in str(info.value)


@pytest.mark.parametrize("name", ["HTTPS_PROXY", "ALL_PROXY"])
def test_no_proxy_star_skips_environment_check(clean_proxy_env, name):
    clean_proxy_env.setenv(name, "socks4://h:1")
    clean_proxy_env.setenv("NO_PROXY", "*")
    spy.scrape("a.com")


@pytest.mark.parametrize("value", ["http://", "http://:8080", "http://h:99999"])
def test_environment_proxies_httpx_accepts_still_accepted(clean_proxy_env, value):
    clean_proxy_env.setenv("HTTPS_PROXY" if value.endswith("99999") else "HTTP_PROXY", value)
    spy.scrape("a.com")


def test_custom_fetcher_skips_environment_check(clean_proxy_env):
    clean_proxy_env.setenv("HTTPS_PROXY", "socks4://h:1")
    items, _, _ = scrape(ok_routes(), "shop.test")
    assert len(items) == 2


def test_custom_fetcher_ignores_explicit_proxy():
    items, _, _ = scrape(ok_routes(), "shop.test", proxy="socks5://h:1080")
    assert len(items) == 2


def test_proxy_407_fails_store_without_leaking_credentials(
    clean_proxy_env, recording_proxy, caplog
):
    proxy = recording_proxy(reject_connect=True)
    url = f"http://u:secret@127.0.0.1:{proxy.port}"
    caplog.set_level("DEBUG", logger="shopify_spy")
    run = spy.scrape(STORE, proxy=url, retries=0, bail=None, throttle=False)
    with pytest.raises(spy.ScrapeFailed) as info:
        list(run)
    [store] = run.result.stores
    assert store.status == "no_response"
    assert store.hint == "requests went through a proxy; check that it is reachable"
    assert proxy.records and all(command == "CONNECT" for command, _, _ in proxy.records)
    assert "secret" not in repr(run.result)
    assert "secret" not in str(info.value)
    assert "secret" not in caplog.text
    assert f"Using proxy http://u:***@127.0.0.1:{proxy.port}" in caplog.text


def test_default_transport_resolves_hostnames_for_the_check(clean_proxy_env):
    calls = []
    table = {"shop.example": "8.8.8.8", "internal.test": "10.0.0.5"}

    async def stub(url):
        host = urllib.parse.urlsplit(url).hostname
        calls.append(host)
        return [ipaddress.ip_address(table[host])] if host in table else []

    store = "https://shop.example"
    fetcher = FakeFetcher({bulk_url(1, store): (302, "", {"Location": "https://internal.test/x"})})
    clean_proxy_env.setattr(_address, "resolve", stub)
    clean_proxy_env.setattr(_runner, "CurlCffiFetcher", lambda *a, **kw: fetcher)
    list(spy.scrape(store, **FAST))
    assert set(calls) == {"shop.example", "internal.test"}
    assert not [r.url for r in fetcher.requests if "internal.test" in r.url]

    calls.clear()
    list(spy.scrape(store, fetcher_factory=lambda: fetcher, **FAST))
    assert calls == []


def test_proxy_kept_out_of_option_reprs():
    from shopify_spy._api import ScrapeOptions
    from shopify_spy._runner import RunConfig

    url = "http://u:secret@gw:8000"
    assert "secret" not in repr(ScrapeOptions(proxy=url))
    assert "secret" not in repr(RunConfig(proxy=url))


# --- Impersonation ---


class _CurlLike(FakeFetcher):
    """FakeFetcher with CurlCffiFetcher's capabilities, so the runner retries as it would."""

    capabilities = CurlCffiFetcher.capabilities


def test_impersonate_with_user_agent_raises():
    with pytest.raises(ValueError, match="browser profile; drop user_agent"):
        spy.scrape("a.com", impersonate="chrome", user_agent="foo/1.0")


def test_impersonate_with_user_agent_raises_with_custom_fetcher():
    with pytest.raises(ValueError, match="drop user_agent"):
        spy.scrape("a.com", impersonate="chrome", user_agent="foo/1.0", fetcher_factory=fake({}))


@pytest.mark.parametrize("value", [True, False, 0, ""])
def test_impersonate_rejects_values_that_are_not_a_profile(value):
    with pytest.raises(ValueError, match="impersonate"):
        spy.scrape("a.com", impersonate=value)


def test_unknown_impersonate_target_raises_at_call_time():
    with pytest.raises(ValueError, match="'nosuchbrowser'.*chrome, firefox or safari"):
        spy.scrape("a.com", impersonate="nosuchbrowser")


def test_custom_fetcher_ignores_impersonate(monkeypatch):
    monkeypatch.setitem(sys.modules, "curl_cffi", None)  # neither imported nor validated
    items, _, _ = scrape(ok_routes(), impersonate="nosuchbrowser")
    assert len(items) == 2


def _record_transports(monkeypatch, routes=None):
    """Replace both default transports with fakes; return the constructor calls."""
    made = []

    def curl_fetcher(impersonate, *, verify, proxy):
        made.append(("curl_cffi", impersonate, verify, proxy))
        return _CurlLike(routes or ok_routes())

    def httpx_fetcher(*, verify, proxy):
        made.append(("httpx", verify, proxy))
        return FakeFetcher(routes or ok_routes())

    monkeypatch.setattr(_runner, "CurlCffiFetcher", curl_fetcher)
    monkeypatch.setattr(_runner, "HttpxFetcher", httpx_fetcher)
    return made


def test_default_run_impersonates_chrome(monkeypatch, clean_proxy_env, caplog):
    made = _record_transports(monkeypatch)
    caplog.set_level("INFO", logger="shopify_spy")
    proxy = "http://u:secret@127.0.0.1:8000"
    run = spy.scrape(STORE, verify_tls=False, proxy=proxy, **FAST)
    assert len(list(run)) == 2
    assert made == [("curl_cffi", "chrome", False, proxy)]
    assert "Impersonating chrome, then safari, firefox after a refusal (curl_cffi " in caplog.text
    assert "secret" not in caplog.text


def test_impersonate_picks_the_first_profile(monkeypatch, clean_proxy_env, caplog):
    made = _record_transports(monkeypatch)
    caplog.set_level("INFO", logger="shopify_spy")
    assert len(list(spy.scrape(STORE, impersonate="chrome146", **FAST))) == 2
    assert made == [("curl_cffi", "chrome146", True, None)]
    assert "Impersonating chrome146, then chrome, safari, firefox after" in caplog.text


def test_no_rotate_logs_only_the_first_profile(monkeypatch, clean_proxy_env, caplog):
    _record_transports(monkeypatch)
    caplog.set_level("INFO", logger="shopify_spy")
    assert len(list(spy.scrape(STORE, rotate=False, **FAST))) == 2
    assert "Impersonating chrome (curl_cffi " in caplog.text
    assert "after a refusal" not in caplog.text


def test_user_agent_runs_on_httpx(monkeypatch, clean_proxy_env):
    made = _record_transports(monkeypatch)
    assert len(list(spy.scrape(STORE, user_agent="MyBot/1.0", **FAST))) == 2
    assert made == [("httpx", True, None)]


def test_user_agent_is_sent_unchanged_without_rotation():
    fetcher = FakeFetcher({f"{STORE}/robots.txt": (404, "")}, default_status=403)
    store = _status(fetcher, retries=2, user_agent="MyBot/1.0")
    assert store.status == "forbidden"
    assert {(r.identity.user_agent, r.identity.impersonate) for r in fetcher.requests} == {
        ("MyBot/1.0", None)
    }


@pytest.mark.parametrize("status", [403, 429])
def test_impersonated_refusal_retried_with_the_next_profile(status):
    fetcher = _CurlLike(
        {f"{STORE}/robots.txt": (404, ""), bulk_url(1): Seq([(status, ""), products(2)])}
    )
    items, _, _ = scrape(fetcher)
    assert len(items) == 2
    profiles = [r.identity.impersonate for r in fetcher.requests if r.url == bulk_url(1)]
    assert profiles == ["chrome", "safari"]


def test_impersonated_forbidden_store_switches_profile_once():
    fetcher = _CurlLike({f"{STORE}/robots.txt": (404, "")}, default_status=403)
    store = _status(fetcher, retries=3)
    assert store.status == "forbidden"
    assert store.identity_changes == 1
    profiles = [r.identity.impersonate for r in fetcher.requests if r.url == bulk_url(1)]
    assert profiles == ["chrome", "safari", "safari", "safari"]


def test_identity_changes_reported():
    fetcher = _CurlLike(
        {f"{STORE}/robots.txt": (404, ""), bulk_url(1): Seq([(429, ""), products(2)])}
    )
    _, result, _ = scrape(fetcher)
    assert result.stores[0].identity_changes == 1


def test_rotate_false_keeps_one_identity():
    fetcher = _CurlLike(
        {f"{STORE}/robots.txt": (404, ""), bulk_url(1): Seq([(403, ""), products(2)])}
    )
    _, result, _ = scrape(fetcher, rotate=False)
    assert result.stores[0].identity_changes == 0
    assert {r.identity.impersonate for r in fetcher.requests} == {"chrome"}


def test_each_store_starts_with_the_first_profile():
    other_store = "https://other.test"
    fetcher = _CurlLike(
        {
            bulk_url(1): Seq([(403, ""), products(1)]),
            bulk_url(1, other_store): Seq([(403, ""), products(1)]),
        }
    )
    run = spy.scrape([STORE, other_store], fetcher_factory=lambda: fetcher, **FAST)
    list(run)
    for store in (STORE, other_store):
        sent = [r.identity.impersonate for r in fetcher.requests if r.url == bulk_url(1, store)]
        assert sent == ["chrome", "safari"]
