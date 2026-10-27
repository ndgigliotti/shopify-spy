"""Politeness and resilience policies: throttling, retries, robots.txt, identities."""

from __future__ import annotations

import asyncio
import email.utils
import random
import time
from dataclasses import dataclass, field

from protego import Protego

from shopify_spy.fetch import FetcherCapabilities, Identity
from shopify_spy.user_agents import USER_AGENTS

# Headers Scrapy sends by default; kept so servers see the same requests as before
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}

# Product token used to match robots.txt groups (falls back to "*")
ROBOTS_TOKEN = "shopify-spy"

RETRY_STATUSES = frozenset({500, 502, 503, 504, 522, 524, 408, 429, 403})
RETRY_MAX_DELAY = 60.0  # longest wait before a retry, Retry-After included
ROTATE_ON_STATUSES = frozenset({403})


def now() -> float:
    """The running loop's clock (excludes paused time under the sync API)."""
    return asyncio.get_running_loop().time()


@dataclass
class ThrottleSettings:
    enabled: bool = True
    start_delay: float = 1.0
    max_delay: float = 60.0
    target_concurrency: float = 1.0


class HostSlot:
    """Per-host concurrency and AutoThrottle state.

    The throttle algorithm is adapted from Scrapy's AutoThrottle extension and
    download slots (https://github.com/scrapy/scrapy, BSD-3-Clause).

    One request leaves the slot per gate pass; with a non-zero delay, passes are
    spaced by ``uniform(0.5, 1.5) * delay`` since the previous request.
    """

    def __init__(self, concurrency: int, throttle: ThrottleSettings) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)
        self.throttle = throttle
        self.delay = max(0.0, throttle.start_delay) if throttle.enabled else 0.0
        self.last_seen: float | None = None
        self.paused_until = 0.0
        self._gate = asyncio.Lock()

    async def wait_turn(self) -> None:
        """Wait until the throttle allows the next request to leave this slot."""
        async with self._gate:
            pause = self.paused_until - now()
            if pause > 0:
                await asyncio.sleep(pause)
            if self.delay > 0 and self.last_seen is not None:
                wait = self.delay * random.uniform(0.5, 1.5) - (now() - self.last_seen)
                if wait > 0:
                    await asyncio.sleep(wait)
            self.last_seen = now()

    def adjust(self, latency: float, status: int) -> None:
        """AutoThrottle: move the delay toward latency / target_concurrency."""
        if not self.throttle.enabled:
            return
        target = latency / self.throttle.target_concurrency
        new = max(target, (self.delay + target) / 2.0)
        new = min(max(new, 0.0), self.throttle.max_delay)
        # A non-200 response must not make us faster (errors can be quick to arrive)
        if status != 200 and new <= self.delay:
            return
        self.delay = new

    def pause(self, seconds: float) -> None:
        """Hold every request to this host for ``seconds`` and slow the throttle.

        For a 429: otherwise only the rate-limited request waits, while the host's
        other requests keep arriving and use up their own retries.
        """
        self.paused_until = max(self.paused_until, now() + seconds)
        if self.throttle.enabled:
            doubled = max(self.delay * 2, self.throttle.start_delay)
            self.delay = min(doubled, self.throttle.max_delay)


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
    max_delay: float = RETRY_MAX_DELAY

    def should_retry_status(self, status: int, attempt: int) -> bool:
        return status in self.statuses and attempt < self.retries

    def delay(self, attempt: int, retry_after: float | None, cap: float | None) -> float:
        """Seconds to wait before retry number ``attempt + 1``."""
        if not self.backoff:
            return 0.0
        delay = self.base_delay * (2**attempt) * random.uniform(0.5, 1.5)
        if retry_after is not None:
            delay = max(delay, retry_after)
        limit = self.max_delay if cap is None else min(self.max_delay, cap)
        return max(0.0, min(delay, limit))


def retry_policy_for(capabilities: FetcherCapabilities, retries: int, backoff: bool) -> RetryPolicy:
    """Pick retry behavior for a transport. Browsers don't rotate identities."""
    rotate = frozenset() if not capabilities.applies_identity_headers else ROTATE_ON_STATUSES
    return RetryPolicy(retries=retries, rotate_on=rotate, backoff=backoff)


@dataclass
class IdentityPool:
    """A store's browser identity: one random UA, swapped only after a 403.

    A fixed ``user_agent`` disables rotation.
    """

    user_agent: str | None = None
    agents: list[str] = field(default_factory=lambda: list(USER_AGENTS))
    current: Identity = field(init=False)

    def __post_init__(self) -> None:
        ua = self.user_agent or random.choice(self.agents)
        self.current = Identity(user_agent=ua, headers=dict(DEFAULT_HEADERS))

    @property
    def rotates(self) -> bool:
        return self.user_agent is None and len(self.agents) > 1

    def rotate(self) -> None:
        if not self.rotates:
            return
        previous = self.current.user_agent
        ua = previous
        while ua == previous:
            ua = random.choice(self.agents)
        self.current = Identity(user_agent=ua, headers=dict(DEFAULT_HEADERS))


class RobotsRules:
    """Parsed robots.txt rules for one origin."""

    def __init__(self, text: str | None) -> None:
        self._parser = Protego.parse(text) if text else None

    def allowed(self, url: str, agent: str) -> bool:
        if self._parser is None:
            return True
        return self._parser.can_fetch(url, agent)
