"""Tests for throttling, retry, identity and robots.txt policies."""

import asyncio
import email.utils
import time

import pytest

from shopify_spy import _policy as policy
from shopify_spy._fetch import FetcherCapabilities
from shopify_spy._policy import (
    RETRY_MAX_DELAY,
    HostSlot,
    IdentityPool,
    RetryPolicy,
    RobotsRules,
    ThrottleSettings,
    parse_retry_after,
    retry_policy_for,
)


@pytest.fixture
def no_jitter(monkeypatch):
    monkeypatch.setattr(policy.random, "uniform", lambda a, b: 1.0)


def slot(**throttle) -> HostSlot:
    return HostSlot(16, ThrottleSettings(**throttle))


def scrapy_adjust(delay, latency, status, target_concurrency=1.0, max_delay=60.0):
    """Scrapy 2.14 AutoThrottle._adjust_delay, transcribed."""
    target = latency / target_concurrency
    new = max(target, (delay + target) / 2.0)
    new = min(max(0.0, new), max_delay)
    if status != 200 and new <= delay:
        return delay
    return new


# --- HostSlot.adjust ------------------------------------------------------------------


def test_adjust_moves_halfway_toward_target():
    s = slot(start_delay=1.0)
    s.adjust(0.2, 200)
    assert s.delay == pytest.approx(0.6)


def test_adjust_jumps_up_to_target_when_latency_is_high():
    s = slot(start_delay=1.0)
    s.adjust(10.0, 200)
    assert s.delay == pytest.approx(10.0)


def test_adjust_uses_target_concurrency():
    s = slot(start_delay=0.0, target_concurrency=4.0)
    s.adjust(2.0, 200)
    assert s.delay == pytest.approx(0.5)


def test_adjust_clamped_to_max_delay():
    s = slot(start_delay=1.0, max_delay=2.0)
    s.adjust(30.0, 200)
    assert s.delay == 2.0


def test_adjust_non_200_never_decreases():
    s = slot(start_delay=1.0)
    s.adjust(0.1, 500)
    assert s.delay == 1.0
    s.adjust(4.0, 502)
    assert s.delay == pytest.approx(4.0)


def test_adjust_matches_scrapy_over_a_sequence():
    s = slot(start_delay=1.0, max_delay=5.0, target_concurrency=2.0)
    expected = 1.0
    for latency, status in [(0.1, 200), (3.0, 200), (0.4, 404), (12.0, 502), (0.0, 200)]:
        s.adjust(latency, status)
        expected = scrapy_adjust(expected, latency, status, 2.0, 5.0)
        assert s.delay == pytest.approx(expected)


def test_disabled_throttle_has_zero_delay_and_ignores_adjust():
    s = slot(enabled=False, start_delay=3.0)
    assert s.delay == 0.0
    s.adjust(10.0, 200)
    assert s.delay == 0.0


# --- HostSlot.wait_turn ---------------------------------------------------------------


async def test_wait_turn_spaces_requests_by_delay(no_jitter):
    s = slot(start_delay=0.1)
    loop = asyncio.get_running_loop()
    async with asyncio.timeout(2):
        t0 = loop.time()
        await s.wait_turn()
        t1 = loop.time()
        await s.wait_turn()
        t2 = loop.time()
    assert t1 - t0 < 0.05  # first request leaves immediately
    assert t2 - t1 == pytest.approx(0.1, abs=0.05)


async def test_wait_turn_concurrent_waiters_are_serialized(no_jitter):
    s = slot(start_delay=0.05)
    loop = asyncio.get_running_loop()
    times = []

    async def go():
        await s.wait_turn()
        times.append(loop.time())

    async with asyncio.timeout(2):
        await asyncio.gather(*(go() for _ in range(3)))
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(gap >= 0.04 for gap in gaps)


async def test_pause_holds_requests_and_slows_the_throttle():
    s = slot(start_delay=0.0, max_delay=0.5)
    s.pause(0.2)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with asyncio.timeout(2):
        await s.wait_turn()
    assert loop.time() - t0 >= 0.15
    # Slowed even from a zero start delay (to the 1 s minimum, clamped to max_delay)
    assert s.delay == s.floor == 0.5


@pytest.fixture
def clock(monkeypatch):
    """A manual clock for HostSlot: advance it with clock.t += seconds."""

    class Clock:
        t = 1000.0

    monkeypatch.setattr(policy, "now", lambda: Clock.t)
    return Clock


async def test_pause_sets_a_floor_that_eases_back_down():
    # Fast responses after a rate limit must not jump back to the refused rate,
    # but the host recovers gradually
    s = slot(start_delay=1.0, max_delay=60.0)
    s.delay = 0.2
    s.pause(0)
    assert s.delay == s.floor == 1.0  # doubled 0.4, raised to start_delay
    s.adjust(latency=0.05, status=200)
    assert s.delay == pytest.approx(0.98)  # straight to the decayed floor, not 0.05
    for _ in range(33):
        s.adjust(latency=0.05, status=200)
    assert s.floor == pytest.approx(0.5, abs=0.02)  # halves in ~34 successes
    s.adjust(latency=0.05, status=429)
    assert s.floor == pytest.approx(0.5, abs=0.02)  # only successes lower it


@pytest.mark.parametrize("pause", [5.0, 0.0], ids=["backoff", "no-pause"])
def test_refusals_without_an_answer_between_double_once(clock, pause):
    # In-flight stragglers and a cooldown that refuses everything are one signal:
    # they pause the host but don't keep halving its rate (a live Shopify store
    # drove the delay to 60 s and its run to 0 items this way)
    s = slot(start_delay=1.0, max_delay=60.0)
    for _ in range(6):
        s.pause(pause)
        clock.t += 10.0
    assert s.floor == 2.0  # 1.0 doubled once
    assert s.paused_until >= clock.t - 10.0 + pause


def test_refusal_after_answers_at_the_slower_rate_doubles_again(clock):
    s = slot(start_delay=1.0, max_delay=60.0)
    s.pause(1.0)
    s.adjust(0.1, 200)  # the slower rate worked for a while...
    s.pause(1.0)  # ...then was refused too
    assert s.floor == pytest.approx(2.0 * 0.98 * 2)  # eased once, then doubled


def test_blocks_and_errors_dont_count_as_answers(clock):
    s = slot(start_delay=1.0, max_delay=60.0)
    s.pause(1.0)
    for status in (403, 430, 500):
        s.adjust(0.1, status)
    s.pause(1.0)
    assert s.floor == 2.0


async def test_wait_turn_honors_a_pause_extended_while_waiting():
    s = slot(enabled=False)
    loop = asyncio.get_running_loop()
    s.pause(0.1)
    t0 = loop.time()
    waiter = asyncio.create_task(s.wait_turn())
    await asyncio.sleep(0.05)
    s.pause(0.2)  # extended to ~0.25 from t0 while the waiter sleeps
    async with asyncio.timeout(2):
        await waiter
    assert loop.time() - t0 >= 0.2


def test_adjust_ignores_429_but_follows_slow_503():
    # pause() owns 429s; a slow 503 still says the host is overloaded
    s = slot(start_delay=1.0)
    s.adjust(30.0, 429)
    assert s.delay == 1.0
    s.adjust(12.0, 503)
    assert s.delay == 12.0


def test_floor_eases_only_when_the_host_answered_normally(clock):
    s = slot(start_delay=1.0)
    s.pause(0)
    s.adjust(0.05, 404)  # a missing page is still the host answering fine
    assert s.floor == pytest.approx(2.0 * 0.98)
    for blocked in (500, 403, 430):  # errors and blocks aren't a reason to speed up
        s.adjust(0.05, blocked)
    assert s.floor == pytest.approx(2.0 * 0.98)


async def test_pause_without_throttle_keeps_zero_delay():
    s = slot(enabled=False)
    s.pause(0.05)
    assert s.delay == 0.0
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    async with asyncio.timeout(2):
        await s.wait_turn()
    assert loop.time() - t0 >= 0.03


async def test_wait_turn_immediate_when_throttle_disabled():
    s = slot(enabled=False)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    for _ in range(5):
        await s.wait_turn()
    assert loop.time() - t0 < 0.05


# --- parse_retry_after ----------------------------------------------------------------


def test_parse_retry_after_seconds():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 5 ") == 5.0


def test_parse_retry_after_http_date():
    future = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert parse_retry_after(future) == pytest.approx(30, abs=2)


def test_parse_retry_after_past_date_is_zero():
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


@pytest.mark.parametrize("value", [None, "", "soon", "-5", "Not a date, 99"])
def test_parse_retry_after_junk(value):
    assert parse_retry_after(value) is None


# --- RetryPolicy ----------------------------------------------------------------------


def test_retry_delay_zero_without_backoff():
    retry = RetryPolicy(backoff=False)
    assert retry.delay(0, None, None) == 0.0
    assert retry.delay(3, 30.0, None) == 0.0


def test_retry_delay_exponential(no_jitter):
    retry = RetryPolicy(base_delay=0.5)
    assert [retry.delay(n, None, None) for n in range(4)] == [0.5, 1.0, 2.0, 4.0]


def test_retry_delay_jitter_range():
    retry = RetryPolicy(base_delay=1.0)
    for _ in range(50):
        assert 0.5 <= retry.delay(0, None, None) <= 1.5


def test_retry_delay_honors_retry_after(no_jitter):
    retry = RetryPolicy(base_delay=0.5)
    assert retry.delay(0, 10.0, None) == 10.0


def test_rate_limited_retry_waits_longer_without_retry_after(no_jitter):
    retry = RetryPolicy(base_delay=0.5, rate_limit_base_delay=5.0)
    assert retry.delay(0, None, None, rate_limited=True) == 5.0
    assert retry.delay(2, None, None, rate_limited=True) == 20.0
    assert retry.delay(0, None, None) == 0.5  # other errors keep the short backoff


def test_rate_limited_retry_follows_retry_after(no_jitter):
    retry = RetryPolicy(base_delay=0.5, rate_limit_base_delay=5.0)
    assert retry.delay(0, 1.0, None, rate_limited=True) == 1.0  # the server knows best


def test_retry_delay_capped_by_max_delay(no_jitter):
    retry = RetryPolicy(base_delay=0.5, max_delay=5.0)
    assert retry.delay(0, 100.0, None) == 5.0
    assert retry.delay(20, None, None) == 5.0


def test_retry_delay_capped_by_cap(no_jitter):
    retry = RetryPolicy(base_delay=0.5, max_delay=60.0)
    assert retry.delay(0, 100.0, 2.0) == 2.0
    assert retry.delay(0, 100.0, 0.0) == 0.0


def test_should_retry_status():
    retry = RetryPolicy(retries=2)
    assert retry.should_retry_status(503, 0)
    assert retry.should_retry_status(403, 1)
    assert not retry.should_retry_status(503, 2)
    assert not retry.should_retry_status(404, 0)


def test_retry_policy_for_http_transport_rotates_on_403():
    retry = retry_policy_for(FetcherCapabilities(), retries=3, backoff=False)
    assert retry.rotate_on == frozenset({403})
    assert (retry.retries, retry.backoff) == (3, False)
    # The retry cap doesn't follow throttle settings (throttle_max_delay=0 kept retries)
    assert retry.max_delay == RETRY_MAX_DELAY


def test_retry_policy_for_browser_does_not_rotate():
    caps = FetcherCapabilities(applies_identity_headers=False, renders_js=True)
    retry = retry_policy_for(caps, retries=2, backoff=True)
    assert retry.rotate_on == frozenset()
    assert 403 in retry.statuses  # still retried, just without rotation


# --- IdentityPool ---------------------------------------------------------------------


def test_identity_fixed_user_agent_never_rotates():
    pool = IdentityPool(user_agent="fixed/1.0")
    assert not pool.rotates
    pool.rotate()
    assert pool.current.user_agent == "fixed/1.0"


def test_identity_rotate_picks_different_agent():
    pool = IdentityPool(agents=["a", "b", "c"])
    assert pool.rotates
    for _ in range(20):
        before = pool.current.user_agent
        pool.rotate()
        assert pool.current.user_agent != before
        assert pool.current.user_agent in {"a", "b", "c"}


def test_identity_single_agent_cannot_rotate():
    pool = IdentityPool(agents=["only"])
    assert not pool.rotates
    pool.rotate()
    assert pool.current.user_agent == "only"


def test_identity_default_headers():
    pool = IdentityPool()
    assert pool.current.user_agent
    assert pool.current.headers["Accept"].startswith("text/html")
    assert pool.current.headers["Accept-Language"] == "en"
    pool.rotate()
    assert "Accept" in pool.current.headers


# --- RobotsRules ----------------------------------------------------------------------


@pytest.mark.parametrize("text", [None, ""])
def test_robots_missing_allows_everything(text):
    assert RobotsRules(text).allowed("https://a.com/anything", "shopify-spy")


def test_robots_wildcard_and_end_anchor():
    rules = RobotsRules("User-agent: *\nDisallow: /*.json$\nDisallow: /collections/*sort_by*\n")
    assert not rules.allowed("https://a.com/products.json", "shopify-spy")
    assert rules.allowed("https://a.com/products.json?page=2", "shopify-spy")
    assert rules.allowed("https://a.com/products/hat", "shopify-spy")
    assert not rules.allowed("https://a.com/collections/all?sort_by=price", "shopify-spy")
    assert rules.allowed("https://a.com/collections/all", "shopify-spy")


def test_robots_token_falls_back_to_star():
    rules = RobotsRules("User-agent: *\nDisallow: /checkout\n")
    assert not rules.allowed("https://a.com/checkout", policy.ROBOTS_TOKEN)
    assert rules.allowed("https://a.com/products.json", policy.ROBOTS_TOKEN)


def test_robots_specific_group_beats_star():
    rules = RobotsRules("User-agent: shopify-spy\nDisallow: /a\n\nUser-agent: *\nDisallow: /b\n")
    assert not rules.allowed("https://a.com/a", "shopify-spy")
    assert rules.allowed("https://a.com/b", "shopify-spy")
    assert not rules.allowed("https://a.com/b", "OtherBot")


def test_robots_html_page_has_no_rules():
    rules = RobotsRules("<html><body><h1>404 Not Found</h1></body></html>")
    assert rules.allowed("https://a.com/products.json", "shopify-spy")
