"""Tests for StoreSession and HostRegistry against FakeFetcher."""

import asyncio
import ipaddress
import urllib.parse

import pytest

from shopify_spy import _address
from shopify_spy._fetch import (
    FakeFetcher,
    FetchConnectionError,
    FetchTimeout,
    FetchTLSError,
    Seq,
)
from shopify_spy._policy import IdentityPool, RetryPolicy, ThrottleSettings, now
from shopify_spy._results import StoreStatus, diagnose
from shopify_spy._runner import _direct_resolver
from shopify_spy._session import (
    IMAGE,
    MAX_REDIRECTS,
    PROBE,
    HostRegistry,
    RequestFailed,
    ShutdownRequested,
    SingleFlight,
    StoreSession,
)
from shopify_spy._session import STORE as STORE_KIND
from shopify_spy._woocommerce import VARIATIONS
from tests.test_address import NON_PUBLIC_URLS, PUBLIC_URLS

STORE = "https://shop.test"
DISALLOW_PRIVATE = (200, "User-agent: *\nDisallow: /private\n")


def make_session(
    routes=None,
    *,
    robots=True,
    retry=None,
    identity=None,
    registry=None,
    store_url="https://shop.example",
    resolver=None,
    request_timeout=5,
) -> tuple[StoreSession, FakeFetcher]:
    fetcher = FakeFetcher(routes or {})
    registry = registry or HostRegistry(
        concurrent_requests=16,
        throttle=ThrottleSettings(enabled=False),
        respect_robots_txt=robots,
        resolver=resolver,
    )
    session = StoreSession(
        fetcher=fetcher,
        registry=registry,
        retry=retry or RetryPolicy(retries=2, backoff=False),
        identity=identity or IdentityPool(),
        request_timeout=request_timeout,
        store_url=store_url,
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
        },
        store_url="http://127.0.0.1:8001",
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


# --- Non-public destinations ----------------------------------------------------------

PUBLIC_STORE = "https://shop.example"
METADATA = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"


def static_resolver(table, calls=None):
    """A resolver mapping hostnames to address lists; unknown names fail ([])."""

    async def resolver(url):
        host = urllib.parse.urlsplit(url).hostname
        if calls is not None:
            calls.append(host)
        return [ipaddress.ip_address(a) for a in table.get(host, [])]

    return resolver


def robots_of(url):
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


async def test_redirect_to_metadata_address_refused(caplog):
    session, fetcher = make_session(
        {f"{PUBLIC_STORE}/products.json": (302, "", {"Location": METADATA})}
    )
    with pytest.raises(RequestFailed) as excinfo:
        await get(session, f"{PUBLIC_STORE}/products.json")
    assert excinfo.value.cause == "refused: 169.254.169.254 is not a public address"
    assert not [r.url for r in fetcher.requests if "169.254.169.254" in r.url]
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == [f"Refused request to {METADATA}: 169.254.169.254 is not a public address"]


@pytest.mark.parametrize("url", NON_PUBLIC_URLS)
async def test_non_public_destination_refused(url):
    session, fetcher = make_session({url: (200, "secret"), robots_of(url): (200, "")})
    with pytest.raises(RequestFailed) as excinfo:
        await get(session, url)
    assert excinfo.value.cause.startswith("refused:")
    assert fetcher.count(url) == 0
    assert fetcher.count(robots_of(url)) == 0
    assert session.stats.requests == 0


@pytest.mark.parametrize("url", PUBLIC_URLS)
async def test_public_destination_fetched(url):
    session, fetcher = make_session({url: (200, "ok")})
    assert (await get(session, url)).status == 200
    assert fetcher.count(url) == 1


@pytest.mark.parametrize(
    ("store_url", "resolver"),
    [
        ("http://127.0.0.1:8001", None),
        ("http://localhost:8000", None),
        ("http://lan.test", static_resolver({"lan.test": ["192.168.1.10"]})),
    ],
)
@pytest.mark.parametrize("target", ["http://localhost:8001/", "http://10.0.0.5/"])
async def test_private_store_may_reach_private_addresses(store_url, resolver, target):
    session, fetcher = make_session(
        {f"{store_url}/go": (302, "", {"Location": target}), target: (200, "ok")},
        store_url=store_url,
        resolver=resolver,
    )
    assert (await get(session, f"{store_url}/go")).status == 200
    assert fetcher.count(target) == 1


async def test_hostname_resolving_to_private_address_refused():
    calls = []
    session, fetcher = make_session(
        {f"{PUBLIC_STORE}/go": (302, "", {"Location": "https://internal.test/x"})},
        resolver=static_resolver(
            {"shop.example": ["8.8.8.8"], "internal.test": ["10.0.0.5"]}, calls
        ),
    )
    with pytest.raises(RequestFailed) as excinfo:
        await get(session, f"{PUBLIC_STORE}/go")
    assert excinfo.value.cause == (
        "refused: internal.test resolves to 10.0.0.5, not a public address"
    )
    assert not [r.url for r in fetcher.requests if "internal.test" in r.url]
    # The store origin is checked for private status and requested: one lookup
    assert calls == ["shop.example", "internal.test"]


async def test_hostname_with_mixed_answers_refused():
    session, fetcher = make_session(
        resolver=static_resolver({"mixed.test": ["8.8.8.8", "10.0.0.5"]})
    )
    with pytest.raises(RequestFailed, match="resolves to 10.0.0.5"):
        await get(session, "https://mixed.test/x")
    assert fetcher.requests == []


async def test_failed_lookup_not_refused():
    session, fetcher = make_session(
        {"https://nowhere.test/x": FetchConnectionError("no such host")},
        resolver=static_resolver({}),
    )
    with pytest.raises(RequestFailed) as excinfo:
        await get(session, "https://nowhere.test/x")
    assert isinstance(excinfo.value.cause, FetchConnectionError)
    assert fetcher.count("https://nowhere.test/x") == 3


async def test_origin_resolved_once():
    calls = []
    session, fetcher = make_session(
        {"https://cdn.test/a": (200, "a"), "https://cdn.test/b": (200, "b")},
        resolver=static_resolver({"cdn.test": ["8.8.8.8"]}, calls),
    )
    await get(session, "https://cdn.test/a")
    await get(session, "https://cdn.test/b")
    assert calls.count("cdn.test") == 1
    assert fetcher.count("https://cdn.test/b") == 1


async def test_without_resolver_names_are_not_looked_up():
    session, fetcher = make_session(
        {
            f"{PUBLIC_STORE}/go": (302, "", {"Location": "https://internal.test/x"}),
            "https://internal.test/x": (200, "ok"),
        }
    )
    assert (await get(session, f"{PUBLIC_STORE}/go")).status == 200
    assert fetcher.count("https://internal.test/x") == 1
    with pytest.raises(RequestFailed, match="refused"):
        await get(session, "http://10.0.0.5/")
    with pytest.raises(RequestFailed, match="refused"):
        await get(session, "http://localhost/")


async def test_store_with_mixed_answers_is_not_private():
    session, fetcher = make_session(
        {
            "https://shop.test/": (200, "home"),
            "https://shop.test/go": (
                302,
                "",
                {"Location": "http://169.254.169.254/latest/meta-data/"},
            ),
        },
        store_url="https://shop.test",
        resolver=static_resolver({"shop.test": ["8.8.8.8", "10.0.0.5"]}),
    )
    with pytest.raises(RequestFailed, match="refused: shop.test resolves to 10.0.0.5"):
        await get(session, "https://shop.test/")
    with pytest.raises(RequestFailed, match="refused"):
        await get(session, "https://shop.test/go")
    assert not [r.url for r in fetcher.requests if "169.254.169.254" in r.url]
    assert fetcher.requests == []
    status = diagnose(
        items=0, errored=False, truncated=False, finish_reason="finished", stats=session.stats
    )
    assert status is StoreStatus.no_response


async def test_stalled_lookup_counts_as_failed():
    calls = []

    async def slow(url):
        calls.append(url)
        await asyncio.sleep(5)
        return [ipaddress.ip_address("10.0.0.5")]

    session, fetcher = make_session(
        {"https://slow.test/x": (200, "ok"), "https://slow.test/y": (200, "ok")},
        resolver=slow,
        request_timeout=0.2,
    )
    async with asyncio.timeout(1):
        assert (await session.get("https://slow.test/x")).status == 200
        assert (await session.get("https://slow.test/y")).status == 200
    assert fetcher.count("https://slow.test/x") == 1
    # The store's own lookup stalls too; each origin waits once
    assert calls == ["https://shop.example", "https://slow.test/x"]


async def test_direct_resolver_skips_proxied_urls(clean_proxy_env):
    calls = []

    async def stub(url):
        calls.append(url)
        return [ipaddress.ip_address("10.0.0.5")]

    clean_proxy_env.setattr(_address, "resolve", stub)
    clean_proxy_env.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    clean_proxy_env.setenv("NO_PROXY", "direct.test")

    assert await _direct_resolver("http://proxy.test:3128")("https://direct.test/") == []
    resolver = _direct_resolver(None)
    assert await resolver("https://proxied.test/") == []
    assert await resolver("https://direct.test/") == [ipaddress.ip_address("10.0.0.5")]
    assert calls == ["https://direct.test/"]


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


@pytest.mark.parametrize("kind", [STORE_KIND, PROBE])
async def test_429_rotates_user_agent(kind):
    session, fetcher = make_session(
        {f"{STORE}/p": Seq([(429, ""), (200, "ok")])},
        robots=False,
        identity=IdentityPool(agents=["ua-1", "ua-2"]),
    )
    assert (await get(session, f"{STORE}/p", kind=kind)).status == 200
    first, second = [r.identity.user_agent for r in fetcher.requests]
    assert first != second


async def test_robots_429_rotates_user_agent():
    session, fetcher = make_session(
        {f"{STORE}/robots.txt": Seq([(429, ""), (200, "")]), f"{STORE}/p": (200, "ok")},
        identity=IdentityPool(agents=["ua-1", "ua-2"]),
    )
    assert (await get(session, f"{STORE}/p")).status == 200
    first, second = [r.identity.user_agent for r in fetcher.requests if "robots" in r.url]
    assert first != second


async def test_429_on_non_rotating_kind_retries_with_same_user_agent():
    session, fetcher = make_session(
        {f"{STORE}/p": Seq([(429, ""), (200, "ok")])},
        robots=False,
        identity=IdentityPool(agents=["ua-1", "ua-2"]),
    )
    assert (await get(session, f"{STORE}/p", kind=VARIATIONS)).status == 200
    first, second = [r.identity.user_agent for r in fetcher.requests]
    assert first == second


async def test_403_on_non_rotating_kind_is_final():
    session, fetcher = make_session(
        {f"{STORE}/p": Seq([(403, ""), (200, "ok")])},
        robots=False,
        identity=IdentityPool(agents=["ua-1", "ua-2"]),
    )
    assert (await get(session, f"{STORE}/p", kind=VARIATIONS)).status == 403
    assert fetcher.count(f"{STORE}/p") == 1


async def test_429_with_fixed_user_agent_keeps_it():
    session, fetcher = make_session(
        {f"{STORE}/p": Seq([(429, ""), (200, "ok")])},
        robots=False,
        identity=IdentityPool(user_agent="fixed/1.0"),
    )
    await get(session, f"{STORE}/p")
    assert [r.identity.user_agent for r in fetcher.requests] == ["fixed/1.0", "fixed/1.0"]


def refuse_first_together(refusal):
    """A route for two URLs: both first requests get ``refusal`` once both are in flight."""
    both_arrived = asyncio.Event()
    calls: dict[str, int] = {}

    async def route(request):
        calls[request.url] = calls.get(request.url, 0) + 1
        if calls[request.url] > 1:
            return (200, "ok")
        if len(calls) == 2:
            both_arrived.set()
        await both_arrived.wait()
        return refusal

    return route


@pytest.mark.parametrize("status", [403, 429])
async def test_concurrent_refusals_of_one_user_agent_rotate_once(status):
    refuse = refuse_first_together((status, ""))
    session, fetcher = make_session(
        {f"{STORE}/a": refuse, f"{STORE}/b": refuse},
        robots=False,
        identity=IdentityPool(agents=["ua-1", "ua-2"]),
    )
    responses = await asyncio.gather(get(session, f"{STORE}/a"), get(session, f"{STORE}/b"))
    assert [r.status for r in responses] == [200, 200]
    agents = [r.identity.user_agent for r in fetcher.requests]
    assert agents[0] == agents[1]
    assert set(agents[2:]) == {"ua-1", "ua-2"} - {agents[0]}


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


@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_not_capped_by_bail_deadline(status):
    session, _ = make_session(
        {f"{STORE}/p": Seq([(status, "", {"Retry-After": "1"}), (200, "ok")])},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, base_delay=0.0, max_delay=60.0),
    )
    deadline = session.bail_deadline = now() + 0.1
    start = now()
    assert (await get(session, f"{STORE}/p")).status == 200
    assert now() - start >= 0.9
    assert session.bail_deadline - deadline >= 0.9


async def test_overlapping_retry_after_waits_extend_bail_once():
    limit = refuse_first_together((429, "", {"Retry-After": "1"}))
    session, _ = make_session(
        {f"{STORE}/a": limit, f"{STORE}/b": limit},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, base_delay=0.0),
    )
    deadline = session.bail_deadline = now() + 0.1
    await asyncio.gather(get(session, f"{STORE}/a"), get(session, f"{STORE}/b"))
    assert 0.9 <= session.bail_deadline - deadline < 1.5


async def test_cut_short_retry_after_wait_gives_back_its_bail_credit():
    session, _ = make_session(
        {f"{STORE}/p": Seq([(429, "", {"Retry-After": "1"}), (200, "ok")])},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, base_delay=0.0),
    )
    deadline = session.bail_deadline = now() + 0.1
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await get(session, f"{STORE}/p")
    assert 0.1 <= session.bail_deadline - deadline < 0.5


async def test_429_without_retry_after_capped_by_bail_deadline():
    session, _ = make_session(
        {f"{STORE}/p": Seq([(429, ""), (200, "ok")])},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, rate_limit_base_delay=30.0),
    )
    deadline = session.bail_deadline = now() + 0.1
    start = now()
    assert (await get(session, f"{STORE}/p")).status == 200
    assert 0.05 <= now() - start < 0.5
    assert session.bail_deadline == deadline


async def test_final_429_retry_after_extends_bail_deadline():
    session, _ = make_session(
        {f"{STORE}/p": (429, "", {"Retry-After": "1"})},
        robots=False,
        retry=RetryPolicy(retries=0),
    )
    deadline = session.bail_deadline = now() + 0.1
    assert (await get(session, f"{STORE}/p")).status == 429
    assert session.registry.slot(STORE).paused_until - now() >= 0.9
    assert session.bail_deadline - deadline >= 0.9


async def test_429_pauses_the_whole_host():
    # Another request to the rate-limited host waits out the retry delay too
    session, _ = make_session(
        {f"{STORE}/a": Seq([(429, ""), (200, "ok")]), f"{STORE}/b": (200, "ok")},
        robots=False,
        retry=RetryPolicy(retries=1, backoff=True, rate_limit_base_delay=0.6),  # waits 0.3-0.9s
    )
    loop = asyncio.get_running_loop()
    first = asyncio.create_task(get(session, f"{STORE}/a"))
    while not session.registry.slot(STORE).paused_until:
        await asyncio.sleep(0)
    start = loop.time()
    assert (await get(session, f"{STORE}/b")).status == 200
    assert loop.time() - start >= 0.25
    assert (await first).status == 200


async def test_429_without_retries_left_still_slows_the_host():
    registry = HostRegistry(
        concurrent_requests=16,
        throttle=ThrottleSettings(start_delay=0.0),
        respect_robots_txt=False,
    )
    session, _ = make_session(
        {f"{STORE}/p": (429, "")}, retry=RetryPolicy(retries=0), registry=registry
    )
    assert (await get(session, f"{STORE}/p")).status == 429
    assert registry.slot(STORE).floor >= 1.0


async def test_429_without_retries_left_honors_retry_after():
    # The host's other requests must wait out the server's Retry-After too
    session, _ = make_session(
        {f"{STORE}/p": (429, "", {"Retry-After": "30"})},
        robots=False,
        retry=RetryPolicy(retries=0),
    )
    assert (await get(session, f"{STORE}/p")).status == 429
    assert session.registry.slot(STORE).paused_until - now() >= 25


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


async def test_shutdown_stops_before_address_lookup():
    calls = []
    session, fetcher = make_session(
        {f"{STORE}/p": (200, "ok")}, resolver=static_resolver({}, calls)
    )
    session.registry.shutdown.set()
    with pytest.raises(ShutdownRequested):
        await get(session, f"{STORE}/p")
    assert calls == []
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


# --- Rate limits ---------------------------------------------------------------------


async def test_rate_limits_count_requests_that_end_on_a_429():
    session, _ = make_session(
        {
            f"{STORE}/limited": (429, ""),
            f"{STORE}/recovered": Seq([(429, ""), (200, "")]),
            f"{STORE}/then_error": Seq([(429, ""), FetchConnectionError("reset")]),
            f"{STORE}/robots.txt": (429, ""),
        }
    )
    assert (await get(session, f"{STORE}/limited")).status == 429
    assert (await get(session, f"{STORE}/recovered")).status == 200
    with pytest.raises(RequestFailed):
        await get(session, f"{STORE}/then_error")
    # robots.txt's 429 counts, though it isn't in by_status; /limited's 3 attempts count once
    assert session.stats.rate_limits == 2
    assert session.stats.by_status[429] == 5  # 3 + 1 + 1


async def test_rate_limit_counted_when_its_retry_wait_is_cancelled():
    session, _ = make_session(
        {f"{STORE}/limited": (429, "")},
        robots=False,
        retry=RetryPolicy(retries=2, backoff=True, rate_limit_base_delay=60.0),
    )
    task = asyncio.create_task(session.get(f"{STORE}/limited"))
    await asyncio.sleep(0.05)  # the first 429 arrived; the retry waits
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.stats.rate_limits == 1


async def test_image_429s_do_not_count_as_rate_limits():
    """A probe's 429 pauses the host and holds up the store; an image's comes after
    its item was scraped."""
    session, _ = make_session(
        {f"{STORE}/probe": (429, ""), f"{STORE}/image": (429, "")}, robots=False
    )
    await get(session, f"{STORE}/probe", kind=PROBE)
    await get(session, f"{STORE}/image", kind=IMAGE)
    assert session.stats.rate_limits == 1
