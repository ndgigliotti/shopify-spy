from unittest.mock import patch

from scrapy.http import Request, Response

from shopify_spy.middlewares import SWAP_ON_STATUSES, UserAgentMiddleware
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


def test_user_agents_look_like_browsers():
    for ua in USER_AGENTS:
        assert ua.startswith("Mozilla/5.0"), f"Unexpected UA format: {ua}"


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
