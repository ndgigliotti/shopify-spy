"""Politeness and resilience policies: throttling, retries, robots.txt, identities."""

from __future__ import annotations

import asyncio
import email.utils
import math
import random
import time
from collections.abc import Collection
from dataclasses import dataclass, field

from protego import Protego

from shopify_spy._fetch import IMPERSONATE_PROFILES, FetcherCapabilities, Identity, Response

# Headers Scrapy sends by default; kept so servers see the same requests as before
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}

# Product token whose robots.txt group always applies, whatever user agent is sent
# (falls back to "*"); a custom user agent's group applies too (#80)
ROBOTS_TOKEN = "shopify-spy"

RETRY_STATUSES = frozenset({500, 502, 503, 504, 522, 524, 408, 429, 403})
RETRY_MAX_DELAY = 60.0  # longest wait before a retry; a longer Retry-After pauses the host instead
ROTATE_ON_STATUSES = frozenset({403, 429})
# "Slow down" responses: they pause the host and raise its delay floor (HostSlot.pause)
RATE_LIMIT_STATUSES = frozenset({429, 503})
# Cloudflare marks its challenge responses with this header; older challenge pages
# only have the interstitial's title
_CHALLENGE_TITLE = b"<title>Just a moment...</title>"


def is_challenge(response: Response) -> bool:
    """Whether a response is a bot challenge page, which only a real browser that runs
    its script can pass: no retry or identity change gets past it."""
    if response.headers.get("cf-mitigated", "").strip().lower() == "challenge":
        return True
    return response.status in (403, 429, 503) and _CHALLENGE_TITLE in response.body[:65536]


def now() -> float:
    """The running loop's clock (excludes paused time under the sync API)."""
    return asyncio.get_running_loop().time()


@dataclass
class ThrottleSettings:
    enabled: bool = True
    start_delay: float = 1.0
    max_delay: float = 60.0
    # Minimum gaps between request starts while enabled (pages and API, images)
    min_delay: float = 1.0
    image_delay: float = 0.25
    jitter: float = 0.5  # spread of the spacing between request starts, 0 to 1
    target_concurrency: float = 1.0


class HostSlot:
    """Per-host concurrency and AutoThrottle state.

    The throttle algorithm is adapted from Scrapy's AutoThrottle extension and
    download slots (https://github.com/scrapy/scrapy, BSD-3-Clause).

    One request leaves the slot per gate pass. While throttling is enabled, passes are
    spaced since the previous request by the larger of the delay and the minimum gap
    times ``uniform(low, 1 + jitter)``, where ``low`` is the larger of ``1 - jitter``
    and the minimum gap divided by that pace (``jitter`` defaults to 0.5). The minimum
    gap is the larger of robots.txt Crawl-delay and ``min_delay`` (``image_delay`` for an
    image download); it floors the spacing, not the adaptive delay. Without throttling,
    only the Crawl-delay spaces requests, unjittered.

    Rate limits work like TCP congestion control: a rate limit doubles a delay floor
    (multiplicative decrease of the rate) and each answered request lowers it a
    little, so the host speeds up gradually until it pushes back again. Once slowed,
    the host only doubles again after it has answered normally a request sent at the
    slower rate: refusals before that come from requests sent at the old rate or from
    a cooldown that refuses everything for a while, and waiting is the answer to both.
    Answers to requests sent before the slowdown don't count either way.
    """

    FLOOR_DECAY = 0.98  # per answered request: the floor halves in ~34 of them
    MIN_RATE_LIMITED_DELAY = 1.0  # a rate limit slows the host to at least this

    def __init__(self, concurrency: int, throttle: ThrottleSettings) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)
        self.throttle = throttle
        self.crawl_delay = 0.0  # robots.txt Crawl-delay, applied even without throttling
        self.delay = (
            min(max(throttle.start_delay, 0.0), throttle.max_delay) if throttle.enabled else 0.0
        )
        self.floor = 0.0  # raised by pause(); adjust() never goes below it
        self.last_seen: float | None = None
        self.paused_until = 0.0
        self.slowed_at = float("-inf")  # when pause() last doubled the delay
        # Whether a request sent since then was answered normally
        self.answered_since_slowdown = True
        self._gate = asyncio.Lock()

    def _min_gap(self, image: bool = False) -> float:
        """The hard minimum gap before a request (an image download if ``image``)."""
        throttle = self.throttle
        floor = throttle.image_delay if image else throttle.min_delay
        minimum = min(floor, throttle.max_delay) if throttle.enabled else 0.0
        return max(self.crawl_delay, minimum)

    def set_crawl_delay(self, seconds: float) -> None:
        """Space requests by at least ``seconds`` (a shared slot keeps the largest)."""
        self.crawl_delay = max(self.crawl_delay, seconds)
        if self.throttle.enabled:
            self.delay = max(self.delay, self.crawl_delay)

    async def wait_turn(self, image: bool = False) -> float:
        """Wait until the throttle allows the next request to leave; return its send time."""
        async with self._gate:
            # A rate limit can extend the pause while we sleep, so check again
            while (pause := self.paused_until - now()) > 0:
                await asyncio.sleep(pause)
            if self.last_seen is not None:
                spacing = self._min_gap(image)
                # Never draw below the minimum gap, so sends don't pile on the floor and
                # a delay just above it isn't faster on average than one just below;
                # --no-throttle keeps just the Crawl-delay
                pace = max(self.delay, spacing)
                if self.throttle.enabled:
                    jitter = self.throttle.jitter
                    spacing = random.uniform(max(pace * (1 - jitter), spacing), pace * (1 + jitter))
                wait = spacing - (now() - self.last_seen)
                if wait > 0:
                    await asyncio.sleep(wait)
            self.last_seen = now()
            return self.last_seen

    def adjust(self, latency: float, status: int, sent_at: float = float("inf")) -> None:
        """AutoThrottle: move the delay toward latency / target_concurrency.

        ``sent_at`` is the request's send time from wait_turn.
        """
        if status < 400 or status == 404:  # answered normally (403/430 are blocks)
            self.floor *= self.FLOOR_DECAY
            if sent_at >= self.slowed_at:
                self.answered_since_slowdown = True
        if not self.throttle.enabled or status == 429:
            return  # pause() handles 429s; a quick refusal says nothing about load
        target = latency / self.throttle.target_concurrency
        new = max(target, (self.delay + target) / 2.0)
        new = min(max(new, self.floor, self.crawl_delay), self.throttle.max_delay)
        # A non-200 response must not make us faster (errors can be quick to arrive)
        if status != 200 and new <= self.delay:
            return
        self.delay = new

    def pause(self, seconds: float, image: bool = False) -> None:
        """Hold every request to this host for ``seconds`` and slow it down.

        For a rate limit: otherwise only the limited request waits, while the host's
        other requests keep arriving and use up their own retries. The doubled delay
        becomes the host's floor, so AutoThrottle can't jump straight back to the
        rate that was refused (``image`` if an image download was refused). It doubles
        again only once the host has answered at the slower rate (see the class
        docstring).
        """
        self.paused_until = max(self.paused_until, now() + seconds)
        if not self.answered_since_slowdown:
            return
        if self.throttle.enabled:
            # Double the refused pace: the minimum gap can hold the spacing above the delay
            pace = max(self.delay, self._min_gap(image))
            doubled = max(pace * 2, self.throttle.start_delay, self.MIN_RATE_LIMITED_DELAY)
            self.delay = self.floor = min(doubled, self.throttle.max_delay)
        self.slowed_at = now()
        self.answered_since_slowdown = False


def parse_retry_after(value: str | None) -> float | None:
    """Return seconds from a Retry-After header (delta-seconds or HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


@dataclass
class RetryPolicy:
    retries: int = 2
    statuses: frozenset[int] = RETRY_STATUSES
    rotate_on: frozenset[int] = ROTATE_ON_STATUSES
    backoff: bool = True
    base_delay: float = 0.5
    # Rate limits are measured over seconds, so a sub-second retry just hits them again
    rate_limit_base_delay: float = 5.0
    max_delay: float = RETRY_MAX_DELAY

    def should_retry_status(self, status: int, attempt: int) -> bool:
        return status in self.statuses and attempt < self.retries

    def delay(
        self,
        attempt: int,
        retry_after: float | None,
        cap: float | None,
        rate_limited: bool = False,
    ) -> float:
        """Seconds to wait before retry number ``attempt + 1``."""
        if not self.backoff:
            return 0.0
        # A server's Retry-After says how long is enough; otherwise guess long
        slow = rate_limited and retry_after is None
        base = self.rate_limit_base_delay if slow else self.base_delay
        delay = base * (2**attempt) * random.uniform(0.5, 1.5)
        if retry_after is not None:
            delay = max(delay, retry_after)
        return max(0.0, min(delay, self.limit(cap)))

    def limit(self, cap: float | None) -> float:
        """The longest wait before a retry, given the caller's ``cap`` (if any)."""
        return self.max_delay if cap is None else min(self.max_delay, cap)

    def retry_after_too_long(self, retry_after: float | None, cap: float | None) -> bool:
        """Whether a Retry-After asks for longer than we'd wait before a retry; such
        a request isn't retried early."""
        return retry_after is not None and retry_after > self.limit(cap)


def retry_policy_for(capabilities: FetcherCapabilities, retries: int, backoff: bool) -> RetryPolicy:
    """Pick retry behavior for a transport.

    A transport rotates identities if it sends the identity's user agent or its
    impersonation profile; one that brings its own (a real browser) doesn't.
    """
    rotates = capabilities.applies_identity_headers or capabilities.can_impersonate
    rotate = ROTATE_ON_STATUSES if rotates else frozenset()
    return RetryPolicy(retries=retries, rotate_on=rotate, backoff=backoff)


@dataclass
class IdentityPool:
    """A store's identity: a browser profile, switched only after a 403 or 429, or a
    fixed ``user_agent``, never switched.

    The identity starts as the first of ``profiles``, and each switch moves to the
    next, wrapping around; the profile brings its own user agent. ``rotation=False``
    keeps the first profile.

    A switch tests whether a refusal is specific to the identity (#62: Shopify
    refusing one user agent). If the new profile is refused before it is answered
    normally, the block isn't, and the store stops switching.
    """

    user_agent: str | None = None
    profiles: tuple[str, ...] = IMPERSONATE_PROFILES  # ignored with a user_agent
    rotation: bool = True
    current: Identity = field(init=False)
    stopped: bool = field(default=False, init=False)  # a switched-to profile was refused
    _index: int = field(default=0, init=False, repr=False)
    # The identity switched to, until it is answered normally
    _on_trial: Identity | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.user_agent is not None:
            self.profiles = ()
            self.current = Identity(user_agent=self.user_agent, headers=dict(DEFAULT_HEADERS))
            return
        self.current = Identity(user_agent=None, impersonate=self.profiles[0])

    @property
    def rotates(self) -> bool:
        return self.rotation and not self.stopped and len(self.profiles) > 1

    def answered(self, sent: Identity) -> None:
        """``sent`` got a normal answer: a refusal it had escaped was about identity."""
        if sent == self._on_trial:
            self._on_trial = None

    def refused(self, sent: Identity) -> None:
        """``sent`` got a 403 or 429; refusing a switched-to profile stops the switches."""
        if sent == self._on_trial:
            self.stopped = True

    def rotate(self, exclude: Collection[str] = ()) -> bool:
        """Switch to the next profile not in ``exclude``; False if none is left."""
        if not self.rotates:
            return False
        profiles = self.profiles
        n = len(profiles)
        for step in range(1, n):
            index = (self._index + step) % n
            if profiles[index] not in exclude:
                self._index = index
                self.current = Identity(user_agent=None, impersonate=profiles[index])
                self._on_trial = self.current
                return True
        return False


class RobotsRules:
    """Parsed robots.txt rules for one origin."""

    def __init__(self, text: str | None) -> None:
        self._parser = Protego.parse(text) if text else None

    def allowed(self, url: str, *agents: str) -> bool:
        """Whether every agent's group (each falling back to ``*``) allows the URL."""
        if self._parser is None:
            return True
        return all(self._parser.can_fetch(url, agent) for agent in agents or (ROBOTS_TOKEN,))

    @property
    def sitemaps(self) -> list[str]:
        """The ``Sitemap:`` values as written, so a relative one stays relative."""
        if self._parser is None:
            return []
        return list(self._parser.sitemaps)

    def crawl_delay(self, *agents: str) -> float | None:
        """The longest gap between requests that the agents' groups (each falling back
        to ``*``) ask for; None without a usable positive value."""
        if self._parser is None:
            return None
        delays = [self._parser.crawl_delay(agent) for agent in agents or (ROBOTS_TOKEN,)]
        usable = [d for d in delays if d is not None and math.isfinite(d) and d > 0]
        return float(max(usable)) if usable else None
