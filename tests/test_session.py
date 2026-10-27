"""Tests for StoreSession and HostRegistry against FakeFetcher."""

import asyncio

import pytest

from shopify_spy.fetch import (
    FakeFetcher,
    FetchConnectionError,
    FetchTimeout,
    FetchTLSError,
    Seq,
)
from shopify_spy.policy import IdentityPool, RetryPolicy, ThrottleSettings, now
from shopify_spy.session import (
    IMAGE,
    MAX_REDIRECTS,
    PROBE,
    HostRegistry,
    RequestFailed,
    ShutdownRequested,
    SingleFlight,
    StoreSession,
)

STORE = "https://shop.test"
DISALLOW_PRIVATE = (200, "User-agent: *\nDisallow: /private\n")


def make_session(
    routes=None,
    *,
    robots=True,
    retry=None,
    identity=None,
    registry=None,
) -> tuple[StoreSession, FakeFetcher]:
    fetcher = FakeFetcher(routes or {})
    registry = registry or HostRegistry(
        concurrent_requests=16,
        throttle=ThrottleSettings(enabled=False),
        respect_robots_txt=robots,
    )
    session = StoreSession(
        fetcher=fetcher,
        registry=registry,
        retry=retry or RetryPolicy(retries=2, backoff=False),
        identity=identity or IdentityPool(),
        request_timeout=5,
    )
    return session, fetcher


async def get(session, url, **kwargs):
    async with asyncio.timeout(2):
        return await session.get(url, **kwargs)


# --- Dedupe ---------------------------------------------------------------------------


async def test_duplicate_url_skipped():
    session, fetcher = make_session({f"{STORE}/p?a=1&b=2": (200, "ok")})
    first = await get(session, f"{STORE}/p?a=1&b=2")
    second = await get(session, f"{STORE}/p?b=2&a=1#top")
    assert first.status == 200
    assert second is None
    assert session.stats.duplicates == 1
    assert fetcher.count(f"{STORE}/p?a=1&b=2") == 1


async def test_kind_without_dedupe_fetches_again():
    session, fetcher = make_session({f"{STORE}/p": (200, "ok")})
    await get(session, f"{STORE}/p")
    assert (await get(session, f"{STORE}/p", kind=PROBE)).status == 200
    assert fetcher.count(f"{STORE}/p") == 2
    assert session.stats.duplicates == 0


# --- robots.txt -----------------------------------------------------------------------


async def test_robots_disallow_returns_none_and_counts_store_only():
    session, fetcher = make_session({f"{STORE}/robots.txt": DISALLOW_PRIVATE})
    assert await get(session, f"{STORE}/private/1") is None
    assert session.stats.robots_disallowed == 1
    assert await get(session, f"{STORE}/private/2", kind=PROBE) is None
    assert await get(session, f"{STORE}/private/3", kind=IMAGE) is None
    assert session.stats.robots_disallowed == 1
    assert all("/private" not in r.url for r in fetcher.requests)


async def test_robots_fetched_once_per_origin_under_concurrency():
    async def slow_robots(request):
        await asyncio.sleep(0.05)
        return DISALLOW_PRIVATE

    session, fetcher = make_session({f"{STORE}/robots.txt": slow_robots})
    async with asyncio.timeout(2):
        results = await asyncio.gather(
            *(session.get(f"{STORE}/page/{i}") for i in range(5)),
            session.get(f"{STORE}/private/x"),
        )
    assert fetcher.count(f"{STORE}/robots.txt") == 1
    assert [r.status for r in results[:5]] == [404] * 5
    assert results[5] is None


async def test_robots_404_html_body_allows():
    session, _ = make_session(
        {
            f"{STORE}/robots.txt": (404, "<html><body>Not Found</body></html>"),
            f"{STORE}/private": (200, "ok"),
        }
    )
    assert (await get(session, f"{STORE}/private")).status == 200


async def test_robots_error_status_body_still_parsed():
    # Scrapy parses whatever body comes back, even on a 4xx
    session, _ = make_session({f"{STORE}/robots.txt": (404, "User-agent: *\nDisallow: /\n")})
    assert await get(session, f"{STORE}/anything") is None


async def test_robots_transport_error_allows():
    session, fetcher = make_session(
        {
            f"{STORE}/robots.txt": FetchConnectionError("refused"),
            f"{STORE}/page": (200, "ok"),
        }
    )
    assert (await get(session, f"{STORE}/page")).status == 200
    assert fetcher.count(f"{STORE}/robots.txt") == 3  # retried, then given up


async def test_robots_not_counted_in_store_stats():
    session, _ = make_session({f"{STORE}/robots.txt": (200, ""), f"{STORE}/p": (200, "ok")})
    await get(session, f"{STORE}/p")
    assert session.stats.responses == 1
    assert session.stats.by_status == {200: 1}


async def test_robots_keyed_by_origin_including_port():
    session, fetcher = make_session(
        {
            "http://127.0.0.1:8001/robots.txt": (200, "User-agent: *\nDisallow: /x\n"),
            "http://127.0.0.1:8002/robots.txt": (200, "User-agent: *\nDisallow: /y\n"),
            "http://127.0.0.1:8001/y": (200, "ok"),
            "http://127.0.0.1:8002/x": (200, "ok"),
        }
    )
    assert await get(session, "http://127.0.0.1:8001/x") is None
    assert (await get(session, "http://127.0.0.1:8001/y")).status == 200
    assert (await get(session, "http://127.0.0.1:8002/x")).status == 200
    assert await get(session, "http://127.0.0.1:8002/y") is None
    assert fetcher.count("http://127.0.0.1:8001/robots.txt") == 1
    assert fetcher.count("http://127.0.0.1:8002/robots.txt") == 1


async def test_robots_ignored_when_disabled():
    session, fetcher = make_session(
        {f"{STORE}/robots.txt": DISALLOW_PRIVATE, f"{STORE}/private": (200, "ok")}, robots=False
    )
    assert (await get(session, f"{STORE}/private")).status == 200
    assert fetcher.count(f"{STORE}/robots.txt") == 0


# --- Redirects ------------------------------------------------------------------------


async def test_redirects_followed_hop_by_hop():
    session, fetcher = make_session(
        {
            f"{STORE}/a": (301, "", {"Location": "/b"}),
            f"{STORE}/b": (302, "", {"Location": "https://www.shop.test/c"}),
            "https://www.shop.test/c": (200, "final"),
        }
    )
    response = await get(session, f"{STORE}/a")
    assert response.status == 200
    assert (response.url, response.text) == ("https://www.shop.test/c", "final")
    assert session.stats.responses == 3
    assert session.stats.by_status == {301: 1, 302: 1, 200: 1}
    assert session.stats.successes == 1
    # The new origin's robots.txt was consulted for the last hop
    assert fetcher.count("https://www.shop.test/robots.txt") == 1


async def test_redirect_to_disallowed_host_returns_none():
    session, fetcher = make_session(
        {
            f"{STORE}/go": (302, "", {"Location": "https://other.test/secret"}),
            "https://other.test/robots.txt": (200, "User-agent: *\nDisallow: /secret\n"),
            "https://other.test/secret": (200, "nope"),
        }
    )
    assert await get(session, f"{STORE}/go") is None
    assert session.stats.robots_disallowed == 1
    assert fetcher.count("https://other.test/secret") == 0


async def test_too_many_redirects_raises():
    routes = {f"{STORE}/r{i}": (301, "", {"Location": f"/r{i + 1}"}) for i in range(25)}
    session, fetcher = make_session(routes)
    with pytest.raises(RequestFailed):
        await get(session, f"{STORE}/r0")
    assert fetcher.count(f"{STORE}/r{MAX_REDIRECTS}") == 1


async def test_redirect_to_an_already_requested_url_is_a_duplicate():
    # Like Scrapy's dupefilter: a loop, or several URLs redirecting to one page
    session, fetcher = make_session({f"{STORE}/loop": (302, "", {"Location": "/loop"})})
    assert await get(session, f"{STORE}/loop") is None
    assert fetcher.count(f"{STORE}/loop") == 1
    assert session.stats.duplicates == 1


async def test_unparseable_redirect_location_returns_the_redirect():
    session, _ = make_session({f"{STORE}/x": (302, "", {"Location": "http://[::1"})})
    assert (await get(session, f"{STORE}/x")).status == 302


async def test_twenty_redirects_are_allowed():
    routes = {f"{STORE}/r{i}": (301, "", {"Location": f"/r{i + 1}"}) for i in range(20)}
    routes[f"{STORE}/r20"] = (200, "ok")
    session, _ = make_session(routes)
    assert (await get(session, f"{STORE}/r0")).status == 200


async def test_non_http_location_returned_as_is():
    session, _ = make_session({f"{STORE}/m": (302, "", {"Location": "mailto:a@b.test"})})
    response = await get(session, f"{STORE}/m")
    assert response.status == 302


async def test_follow_redirects_false_returns_redirect():
    session, fetcher = make_session({f"{STORE}/a": (301, "", {"Location": "/b"})})
    response = await get(session, f"{STORE}/a", follow_redirects=False)
    assert response.status == 301
    assert fetcher.count(f"{STORE}/b") == 0


# --- Retries --------------------------------------------------------------------------


async def test_retries_on_retryable_statuses_then_succeeds():
    session, fetcher = make_session({f"{STORE}/p": Seq([(503, ""), (429, ""), (200, "ok")])})
    response = await get(session, f"{STORE}/p")
    assert response.status == 200
    assert fetcher.count(f"{STORE}/p") == 3
    assert session.stats.by_status == {503: 1, 429: 1, 200: 1}


async def test_retries_exhausted_returns_last_response():
    session, fetcher = make_session({f"{STORE}/p": (500, "boom")})
    response = await get(session, f"{STORE}/p")
    assert response.status == 500
    assert fetcher.count(f"{STORE}/p") == 3


async def test_non_retryable_status_returned_immediately():
    session, fetcher = make_session({f"{STORE}/p": (404, "")})
    assert (await get(session, f"{STORE}/p")).status == 404
    assert fetcher.count(f"{STORE}/p") == 1


async def test_403_rotates_user_agent():
    session, fetcher = make_session(
        {f"{STORE}/p": Seq([(403, ""), (200, "ok")])},
        robots=False,
        identity=IdentityPool(agents=["ua-1", "ua-2"]),
    )
    assert (await get(session, f"{STORE}/p")).status == 200
    first, second = [r.identity.user_agent for r in fetcher.requests]
    assert first != second


async def test_403_with_fixed_user_agent_keeps_it():
    session, fetcher = make_session(
        {f"{STORE}/p": Seq([(403, ""), (200, "ok")])},
        robots=False,
        identity=IdentityPool(user_agent="fixed/1.0"),
    )
    await get(session, f"{STORE}/p")
    assert [r.identity.user_agent for r in fetcher.requests] == ["fixed/1.0", "fixed/1.0"]


async def test_503_does_not_rotate_user_agent():
    session, fetcher = make_session(
        {f"{STORE}/p": Seq([(503, ""), (200, "ok")])},
        robots=False,
        identity=IdentityPool(agents=["ua-1", "ua-2"]),
    )
    await get(session, f"{STORE}/p")
    first, second = [r.identity.user_agent for r in fetcher.requests]
    assert first == second


async def test_transport_error_retried_then_succeeds():
    session, _ = make_session({f"{STORE}/p": Seq([FetchTimeout("slow"), (200, "ok")])})
    assert (await get(session, f"{STORE}/p")).status == 200
    assert session.stats.transport_errors == {"FetchTimeout": 1}


async def test_transport_error_exhausted_raises_request_failed():
    session, fetcher = make_session({f"{STORE}/p": FetchConnectionError("reset")})
    with pytest.raises(RequestFailed) as excinfo:
        await get(session, f"{STORE}/p")
    assert isinstance(excinfo.value.cause, FetchConnectionError)
    assert fetcher.count(f"{STORE}/p") == 3
    assert session.stats.transport_errors == {"FetchConnectionError": 3}


async def test_tls_error_not_retried():
    session, fetcher = make_session({f"{STORE}/p": FetchTLSError("bad cert")})
    with pytest.raises(RequestFailed):
        await get(session, f"{STORE}/p")
    assert fetcher.count(f"{STORE}/p") == 1
    assert session.stats.tls_errors == 1


async def test_retry_after_capped_by_bail_deadline():
    session, _ = make_session(
        {f"{STORE}/p": Seq([(429, "", {"Retry-After": "30"}), (200, "ok")])},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, base_delay=0.0, max_delay=60.0),
    )
    session.bail_deadline = now() + 0.1
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert (await get(session, f"{STORE}/p")).status == 200
    assert 0.05 <= loop.time() - start < 0.5


async def test_429_pauses_the_whole_host():
    # Another request to the rate-limited host waits out the retry delay too
    session, _ = make_session(
        {f"{STORE}/a": Seq([(429, ""), (200, "ok")]), f"{STORE}/b": (200, "ok")},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, base_delay=0.6),  # waits 0.3-0.9s
    )
    loop = asyncio.get_running_loop()
    first = asyncio.create_task(get(session, f"{STORE}/a"))
    while not session.registry.slot(STORE).paused_until:
        await asyncio.sleep(0)
    start = loop.time()
    assert (await get(session, f"{STORE}/b")).status == 200
    assert loop.time() - start >= 0.25
    assert (await first).status == 200


async def test_retry_after_capped_by_max_delay():
    session, _ = make_session(
        {f"{STORE}/p": Seq([(503, "", {"Retry-After": "30"}), (200, "ok")])},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, base_delay=0.0, max_delay=0.1),
    )
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert (await get(session, f"{STORE}/p")).status == 200
    assert 0.05 <= loop.time() - start < 0.5


# --- Stats ----------------------------------------------------------------------------


async def test_stats_count_store_requests_only():
    session, _ = make_session(
        {f"{STORE}/ok": (200, "ok"), f"{STORE}/probe": (200, "ok"), f"{STORE}/img": (200, "")},
        robots=False,
    )
    await get(session, f"{STORE}/ok")
    await get(session, f"{STORE}/probe", kind=PROBE)
    await get(session, f"{STORE}/img", kind=IMAGE)
    await get(session, f"{STORE}/missing")
    assert session.stats.responses == 2
    assert session.stats.successes == 1
    assert session.stats.by_status == {200: 1, 404: 1}
    assert session.stats.requests == 4


# --- Shutdown -------------------------------------------------------------------------


async def test_shutdown_stops_new_requests():
    session, fetcher = make_session({f"{STORE}/p": (200, "ok")})
    session.registry.shutdown.set()
    with pytest.raises(ShutdownRequested):
        await get(session, f"{STORE}/p")
    assert fetcher.requests == []


async def test_shutdown_stops_before_retry():
    session = None

    def route(request):
        session.registry.shutdown.set()
        return (503, "")

    session, fetcher = make_session({f"{STORE}/p": route}, robots=False)
    with pytest.raises(ShutdownRequested):
        await get(session, f"{STORE}/p")
    assert fetcher.count(f"{STORE}/p") == 1


# --- HostRegistry slots ---------------------------------------------------------------


async def test_slots_keyed_by_origin():
    # Plan §4.6: all per-host state is keyed by scheme+host+port, so stores served
    # from one IP on different ports (as in the e2e tests) don't share a throttle
    registry = HostRegistry(
        concurrent_requests=16, throttle=ThrottleSettings(enabled=False), respect_robots_txt=True
    )
    assert registry.slot("https://a.test/x") is registry.slot("https://A.test/y")
    assert registry.slot("https://a.test/x") is not registry.slot("http://a.test:8080/y")
    assert registry.slot("https://a.test/x") is not registry.slot("https://b.test/x")


async def test_single_flight_shares_one_fetch_and_survives_owner_cancellation():
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.1)
        return calls

    flight = SingleFlight()
    owner = asyncio.ensure_future(flight.get("k", fetch))
    await asyncio.sleep(0)  # the owner starts fetching
    waiter = asyncio.ensure_future(flight.get("k", fetch))
    await asyncio.sleep(0)
    owner.cancel()  # the waiter must fetch itself, not receive a missing result
    async with asyncio.timeout(2):
        assert await waiter == 2
        assert await flight.get("k", fetch) == 2  # cached afterwards
    assert calls == 2
