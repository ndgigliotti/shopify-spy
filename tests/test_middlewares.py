import re
from unittest.mock import Mock, patch

import pytest
from scrapy.http import Request, Response
from scrapy.http.request import NO_CALLBACK
from scrapy.settings import Settings
from scrapy.statscollectors import MemoryStatsCollector

from shopify_spy.middlewares import SWAP_ON_STATUSES, StoreResponseStats, UserAgentMiddleware
from shopify_spy.user_agents import USER_AGENTS

# --- user_agents list tests ---


def test_user_agents_not_empty():
    assert len(USER_AGENTS) > 0


def test_user_agents_are_strings():
    for ua in USER_AGENTS:
        assert isinstance(ua, str)
        assert len(ua) > 0


def test_user_agents_no_duplicates():
    assert len(USER_AGENTS) == len(set(USER_AGENTS))


_V = r"[1-9]\d*"  # a major version, never zero-padded
_CHROME_KIT = r"AppleWebKit/537\.36 \(KHTML, like Gecko\)"
_SAFARI_KIT = r"AppleWebKit/605\.1\.15 \(KHTML, like Gecko\)"
_SAFARI_VERSION = rf"Version/{_V}\.\d+(?:\.\d+)?"

# Exact shapes of real browser UAs, on the platforms each browser reports; a UA must
# match one in full
_UA_SHAPES = [
    # Desktop Chrome and Edge (reduced UA: major version only, Edge's equal to Chrome's)
    re.compile(
        r"Mozilla/5\.0 \((?:Windows NT 10\.0; Win64; x64|Macintosh; Intel Mac OS X 10_15_7"
        rf"|X11; Linux x86_64)\) {_CHROME_KIT} Chrome/(?P<v>{_V})\.0\.0\.0 Safari/537\.36"
        r"(?: Edg/(?P=v)\.0\.0\.0)?"
    ),
    # Android Chrome
    re.compile(
        rf"Mozilla/5\.0 \(Linux; Android {_V}; K\) {_CHROME_KIT} "
        rf"Chrome/{_V}\.0\.0\.0 Mobile Safari/537\.36"
    ),
    # Android WebView (wv), which adds Version/4.0
    re.compile(
        rf"Mozilla/5\.0 \(Linux; Android {_V}; K; wv\) {_CHROME_KIT} "
        rf"Version/4\.0 Chrome/{_V}\.0\.0\.0 Mobile Safari/537\.36"
    ),
    # Desktop Firefox: rv: equals the Firefox version
    re.compile(
        r"Mozilla/5\.0 \((?:Windows NT 10\.0; Win64; x64|Macintosh; Intel Mac OS X 10\.15"
        rf"|X11; (?:Ubuntu; )?Linux x86_64); rv:(?P<v>{_V}\.\d+)\) Gecko/20100101 Firefox/(?P=v)"
    ),
    # Safari on macOS
    re.compile(
        rf"Mozilla/5\.0 \(Macintosh; Intel Mac OS X 10_15_7\) {_SAFARI_KIT} "
        rf"{_SAFARI_VERSION} Safari/605\.1\.15"
    ),
    # Safari on iOS (iPhone sends "CPU iPhone OS") and iPadOS ("CPU OS")
    re.compile(
        rf"Mozilla/5\.0 \((?:iPhone; CPU iPhone OS|iPad; CPU OS) {_V}(?:_\d+)* like Mac OS X\) "
        rf"{_SAFARI_KIT} {_SAFARI_VERSION} Mobile/\w+ Safari/604\.1"
    ),
]


def _matches_browser_shape(ua: str) -> bool:
    """Whether a UA is formatted exactly the way a real browser sends it."""
    return any(shape.fullmatch(ua) for shape in _UA_SHAPES)


def test_user_agents_match_real_browser_shapes():
    """Every UA in the rotation is formatted the way a real browser sends it."""
    for ua in USER_AGENTS:
        assert _matches_browser_shape(ua), ua


_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
_IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X)"
_IPAD = "Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X)"
_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_WEBVIEW = "Mozilla/5.0 (Linux; Android 10; K; wv)"
_SK = "AppleWebKit/605.1.15 (KHTML, like Gecko)"
_CK = "AppleWebKit/537.36 (KHTML, like Gecko)"
_FF = "Gecko/20100101 Firefox"


@pytest.mark.parametrize(
    ("ua", "valid"),
    [
        # Safari
        pytest.param(f"{_MAC} {_SK} Version/27.0 Safari/605.1.15", True, id="safari"),
        pytest.param(f"{_MAC} {_SK} Version/26.6.1 Safari/605.1.15", True, id="safari-patch"),
        pytest.param(f"{_MAC} {_SK} Version/27 Safari/605.1.15", False, id="safari-bare-major"),
        pytest.param(f"{_MAC} {_CK} Version/27.0 Safari/605.1.15", False, id="safari-wrong-kit"),
        pytest.param(f"{_MAC} {_SK} Version/27.0 Safari/605.1", False, id="safari-short-build"),
        pytest.param(f"{_MAC} {_SK} Safari/605.1.15", False, id="safari-no-version"),
        pytest.param(
            f"{_MAC} {_SK} Version/27.0 Mobile/15E148 Safari/605.1.15",
            False,
            id="safari-mobile-mac",
        ),
        pytest.param(f"{_IPHONE} {_SK} Version/26.0 Mobile/15E148 Safari/604.1", True, id="iphone"),
        pytest.param(f"{_IPAD} {_SK} Version/26.0 Mobile/15E148 Safari/604.1", True, id="ipad"),
        pytest.param(
            f"Mozilla/5.0 (iPad; CPU iPhone OS 18_6 like Mac OS X) {_SK} "
            "Version/26.0 Mobile/15E148 Safari/604.1",
            False,
            id="ipad-iphone-os",
        ),
        pytest.param(f"{_IPHONE} {_SK} Version/26.0 Safari/604.1", False, id="iphone-no-mobile"),
        pytest.param(
            f"{_IPHONE} {_SK} Version/26.0 Mobile/15E148 Safari/605.1.15",
            False,
            id="iphone-mac-build",
        ),
        # Chrome and Edge
        pytest.param(f"{_WIN} {_CK} Chrome/156.0.0.0 Safari/537.36", True, id="chrome"),
        pytest.param(f"{_WIN} {_CK} Chrome/156.0.7000.12 Safari/537.36", False, id="chrome-full"),
        pytest.param(f"{_WIN} {_CK} Chrome/0156.0.0.0 Safari/537.36", False, id="chrome-zero-pad"),
        pytest.param(f"{_WIN} {_CK} Chrom/156.0.0.0 Safari/537.36", False, id="chrome-typo"),
        pytest.param(f"( ) {_CK} Chrome/156.0.0.0 Safari/537.36", False, id="chrome-no-platform"),
        pytest.param(
            f"{_WIN} {_CK} Chrome/156.0.0.0 Mobile Safari/537.36", False, id="chrome-mobile-win"
        ),
        pytest.param(f"{_WIN} {_CK} Chrome/156.0.0.0 Safari/537.36 Edg/156.0.0.0", True, id="edge"),
        pytest.param(
            f"{_WIN} {_CK} Chrome/157.0.0.0 Safari/537.36 Edg/156.0.0.0", False, id="edge-mismatch"
        ),
        pytest.param(f"{_WIN} {_CK} Safari/537.36 Edg/156.0.0.0", False, id="edge-no-chrome"),
        pytest.param(
            f"{_WIN} {_CK} Chrome/156.0.0.0 Safari/537.36 Edg/156.0.3485.54", False, id="edge-full"
        ),
        pytest.param(
            f"{_WEBVIEW} {_CK} Version/4.0 Chrome/156.0.0.0 Mobile Safari/537.36",
            True,
            id="webview",
        ),
        pytest.param(
            f"{_WEBVIEW} {_CK} Version/4.0 Chrome/156.0.0.0 Mobile Safari/537.36 Edg/156.0.0.0",
            False,
            id="webview-edge",
        ),
        pytest.param(
            f"{_WIN} {_CK} Version/4.0 Chrome/156.0.0.0 Safari/537.36",
            False,
            id="webview-token-win",
        ),
        # Firefox
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:158.0) {_FF}/158.0", True, id="firefox-linux"
        ),
        pytest.param(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:158.0) {_FF}/158.0",
            True,
            id="firefox-mac",
        ),
        pytest.param(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; rv:158.0) {_FF}/158.0",
            False,
            id="firefox-chrome-mac-token",
        ),
        pytest.param(
            f"Mozilla/5.0 (garbage; rv:158.0) {_FF}/158.0", False, id="firefox-bad-platform"
        ),
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:153.0) {_FF}/153.01", False, id="firefox-overmatch"
        ),
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:153.0) {_FF}/158.0",
            False,
            id="firefox-rv-mismatch",
        ),
        pytest.param(
            "Mozilla/5.0 (X11; Linux x86_64; rv:158.0) Gecko/158.0 Firefox/158.0",
            False,
            id="firefox-android-gecko",
        ),
        pytest.param(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:158.0) {_FF}/158.0 Chrome/156.0.0.0 Safari/537.36",
            False,
            id="firefox-chrome-mix",
        ),
    ],
)
def test_browser_shape_check(ua, valid):
    """The shape check accepts real browser UAs and rejects malformed ones."""
    assert _matches_browser_shape(ua) is valid


# --- UserAgentMiddleware init tests ---


def test_middleware_picks_ua_from_list():
    mw = UserAgentMiddleware()
    assert mw.current_ua in USER_AGENTS


def test_middleware_init_randomness():
    """Multiple inits should not always pick the same UA."""
    seen = {UserAgentMiddleware().current_ua for _ in range(50)}
    assert len(seen) > 1


# --- process_request: normal requests ---


def test_sets_user_agent_header():
    mw = UserAgentMiddleware()
    request = Request("https://example.com")

    mw.process_request(request)

    assert request.headers["User-Agent"] == mw.current_ua.encode()


def test_same_ua_across_normal_requests():
    mw = UserAgentMiddleware()
    requests = [Request(f"https://example.com/{i}") for i in range(10)]

    for req in requests:
        mw.process_request(req)

    headers = {req.headers["User-Agent"] for req in requests}
    assert len(headers) == 1


def test_no_swap_on_plain_retry():
    """A retry without retry_reason should not swap the UA."""
    mw = UserAgentMiddleware()
    original_ua = mw.current_ua
    request = Request("https://example.com", meta={"retry_times": 1})

    mw.process_request(request)

    assert mw.current_ua == original_ua


# --- process_response: tagging ---


def test_tags_request_on_403():
    mw = UserAgentMiddleware()
    request = Request("https://example.com")
    response = Response("https://example.com", status=403, request=request)

    mw.process_response(request, response)

    assert request.meta["retry_reason"] == 403


def test_does_not_tag_on_200():
    mw = UserAgentMiddleware()
    request = Request("https://example.com")
    response = Response("https://example.com", status=200, request=request)

    mw.process_response(request, response)

    assert "retry_reason" not in request.meta


def test_does_not_tag_on_500():
    mw = UserAgentMiddleware()
    request = Request("https://example.com")
    response = Response("https://example.com", status=500, request=request)

    mw.process_response(request, response)

    assert "retry_reason" not in request.meta


def test_process_response_returns_response():
    mw = UserAgentMiddleware()
    request = Request("https://example.com")
    response = Response("https://example.com", status=403, request=request)

    result = mw.process_response(request, response)

    assert result is response


# --- process_request: 403 retry swap ---


def test_swaps_ua_on_403_retry():
    mw = UserAgentMiddleware()
    original_ua = mw.current_ua
    request = Request("https://example.com", meta={"retry_reason": 403})

    mw.process_request(request)

    assert mw.current_ua != original_ua
    assert mw.current_ua in USER_AGENTS
    assert request.headers["User-Agent"] == mw.current_ua.encode()


def test_no_swap_on_500_retry():
    """A 500 retry should not swap the UA."""
    mw = UserAgentMiddleware()
    original_ua = mw.current_ua
    request = Request("https://example.com", meta={"retry_times": 1, "retry_reason": 500})

    mw.process_request(request)

    assert mw.current_ua == original_ua


def test_swap_persists_for_subsequent_requests():
    """After a 403 swap, all following requests use the new UA."""
    mw = UserAgentMiddleware()

    # Trigger a swap via 403 retry
    retry_req = Request("https://example.com/retry", meta={"retry_reason": 403})
    mw.process_request(retry_req)
    new_ua = mw.current_ua

    # Subsequent normal requests should use the swapped UA
    normal_req = Request("https://example.com/next")
    mw.process_request(normal_req)
    assert normal_req.headers["User-Agent"] == new_ua.encode()


def test_multiple_403_retries_keep_swapping():
    mw = UserAgentMiddleware()
    seen = {mw.current_ua}

    for i in range(1, 20):
        request = Request(f"https://example.com/{i}", meta={"retry_reason": 403})
        mw.process_request(request)
        seen.add(mw.current_ua)

    assert len(seen) > 2


def test_swap_always_picks_different_ua():
    """Each 403 swap must produce a different UA than the current one."""
    mw = UserAgentMiddleware()

    for _ in range(50):
        previous = mw.current_ua
        request = Request("https://example.com", meta={"retry_reason": 403})
        mw.process_request(request)
        assert mw.current_ua != previous


@patch("shopify_spy.middlewares.USER_AGENTS", ["only-one"])
def test_single_ua_list_no_infinite_loop():
    """With only one UA in the list, 403 retry should not loop forever."""
    mw = UserAgentMiddleware()
    request = Request("https://example.com", meta={"retry_reason": 403})

    mw.process_request(request)

    assert mw.current_ua == "only-one"
    assert request.headers["User-Agent"] == b"only-one"


# --- end-to-end: process_response then process_request ---


def test_full_403_cycle():
    """Simulate the full flow: response tags, retry request swaps."""
    mw = UserAgentMiddleware()

    # Initial request
    request = Request("https://example.com")
    mw.process_request(request)
    original_ua = mw.current_ua

    # Server responds with 403 -- process_response tags it
    response = Response("https://example.com", status=403, request=request)
    mw.process_response(request, response)

    # Scrapy's RetryMiddleware would copy the request (including meta)
    retry_request = request.copy()
    retry_request.meta["retry_times"] = 1

    # Retry goes through process_request -- should swap
    mw.process_request(retry_request)
    assert mw.current_ua != original_ua
    assert retry_request.headers["User-Agent"] == mw.current_ua.encode()


def test_full_500_cycle_no_swap():
    """A 500 retry should go through without swapping."""
    mw = UserAgentMiddleware()

    request = Request("https://example.com")
    mw.process_request(request)
    original_ua = mw.current_ua

    # Server responds with 500
    response = Response("https://example.com", status=500, request=request)
    mw.process_response(request, response)

    # Retry
    retry_request = request.copy()
    retry_request.meta["retry_times"] = 1
    mw.process_request(retry_request)

    assert mw.current_ua == original_ua


# --- SWAP_ON_STATUSES ---


def test_swap_on_statuses_contains_403():
    assert 403 in SWAP_ON_STATUSES


def test_swap_on_statuses_excludes_server_errors():
    for code in (500, 502, 503, 504):
        assert code not in SWAP_ON_STATUSES


# --- StoreResponseStats tests ---


def _store_stats():
    crawler = Mock(settings=Settings())
    stats = MemoryStatsCollector(crawler)
    return stats, StoreResponseStats(stats)


def _robots_request(url="https://store.com/robots.txt"):
    # As RobotsTxtMiddleware builds it
    return Request(url, callback=NO_CALLBACK, meta={"dont_obey_robotstxt": True})


def _spider_request(url="https://store.com/products.json"):
    return Request(url, callback=lambda response: None)


def test_store_response_stats_skips_robots_txt():
    stats, mw = _store_stats()
    robots, page = _robots_request(), _spider_request()
    mw.process_response(robots, Response(robots.url, status=404))
    mw.process_response(robots, Response(robots.url, status=403))
    mw.process_response(page, Response(page.url, status=403))
    mw.process_response(page, Response(page.url, status=200))
    assert stats.get_value("shopify_spy/store_response_count") == 2
    assert stats.get_value("shopify_spy/store_success_count") == 1
    assert stats.get_value("shopify_spy/store_response_status_count/403") == 1
    assert stats.get_value("shopify_spy/store_response_status_count/404") is None


def test_store_response_stats_skips_robots_retries_and_redirects():
    """Retried and redirected robots.txt requests keep NO_CALLBACK and stay excluded."""
    stats, mw = _store_stats()
    robots = _robots_request()
    for req in (robots.copy(), robots.replace(url="https://www.store.com/robots.txt")):
        mw.process_response(req, Response(req.url, status=301))
    assert stats.get_value("shopify_spy/store_response_count") is None


def test_store_response_stats_skips_image_downloads():
    """ImagesPipeline requests also use NO_CALLBACK; CDN errors are not store errors."""
    stats, mw = _store_stats()
    image = Request("https://cdn.shopify.com/a.jpg", callback=NO_CALLBACK)
    mw.process_response(image, Response(image.url, status=403))
    assert stats.get_value("shopify_spy/store_response_status_count/403") is None


def test_store_response_stats_counts_default_callback():
    """A spider request without an explicit callback (callback=None) is the store's."""
    stats, mw = _store_stats()
    page = Request("https://store.com/sitemap.xml")
    mw.process_response(page, Response(page.url, status=200))
    assert stats.get_value("shopify_spy/store_success_count") == 1
