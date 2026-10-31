"""Diagnosis edge cases that are awkward to reach through a real scrape."""

from collections import Counter

import pytest

from shopify_spy._results import StoreStatus, diagnose
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
