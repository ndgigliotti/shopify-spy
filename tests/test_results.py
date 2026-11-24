"""Diagnosis edge cases that are awkward to reach through a real scrape."""

from collections import Counter

import pytest

from shopify_spy._results import StoreResult, StoreStatus, diagnose
from shopify_spy._session import StoreStats


@pytest.mark.parametrize("finish_reason", ["closed", "shutdown"])
@pytest.mark.parametrize("by_status", [{429: 1}, {503: 1}])
def test_stopped_mid_retry_is_stopped_not_a_failure(finish_reason, by_status):
    """Ctrl-C or close() during a 429/5xx retry wait: the caller stopped it."""
    stats = StoreStats(responses=1, by_status=Counter(by_status), rate_limits=429 in by_status)
    status = diagnose(
        items=0, errored=False, truncated=False, finish_reason=finish_reason, stats=stats
    )
    assert status == StoreStatus.stopped


def test_proxy_407_is_no_response_not_empty():
    """A proxy's 407 (wrong credentials) means nothing reached the store: a failure."""
    stats = StoreStats(responses=2, by_status=Counter({407: 1, 404: 1}))
    status = diagnose(
        items=0, errored=False, truncated=False, finish_reason="finished", stats=stats
    )
    assert status == StoreStatus.no_response
    assert status.is_failure


def _result(status, via_proxy, by_status=None, suggested=None):
    return StoreResult(
        input_url="a.com",
        url="https://a.com",
        store="a.com",
        platform="shopify",
        items=0,
        status=status,
        complete=True,
        finish_reason="finished",
        suggested_platform=suggested,
        responses_by_status=by_status or {},
        via_proxy=via_proxy,
    )


@pytest.mark.parametrize(
    ("status", "via_proxy", "by_status", "hint"),
    [
        (StoreStatus.no_response, True, {407: 1}, "the proxy refused its credentials (407)"),
        (StoreStatus.no_response, True, {}, "requests went through a proxy"),
        (StoreStatus.tls_error, True, {}, "requests went through a proxy"),
        (StoreStatus.no_response, False, {}, None),
        (StoreStatus.not_found, True, {404: 1}, None),
    ],
)
def test_proxy_hint(status, via_proxy, by_status, hint):
    result = _result(status, via_proxy, by_status)
    if hint is None:
        assert result.hint is None
    else:
        assert result.hint.startswith(hint)


def test_platform_hint_wins_over_proxy_hint():
    result = _result(StoreStatus.no_response, True, suggested="woocommerce")
    assert result.hint.startswith("looks like WooCommerce")
