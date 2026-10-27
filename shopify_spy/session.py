"""The request pipeline: robots.txt, deduplication, slots, throttling, retries, redirects.

``HostRegistry`` holds per-host state shared by every store in a run (robots.txt
rules, concurrency and throttle slots), keyed by origin (scheme://host:port).
``StoreSession`` holds one store's state: identity, cookies, seen URLs, stats.
"""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from shopify_spy.fetch import (
    FetchBadEncoding,
    Fetcher,
    FetchError,
    FetchTLSError,
    FetchTooLarge,
    Request,
    Response,
)
from shopify_spy.policy import (
    ROBOTS_TOKEN,
    HostSlot,
    IdentityPool,
    RetryPolicy,
    RobotsRules,
    ThrottleSettings,
    now,
    parse_retry_after,
)
from shopify_spy.utils import canonicalize_url, origin

logger = logging.getLogger("shopify_spy")

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 20
STORE_CONCURRENCY = 16  # per-store cap, like Scrapy's CONCURRENT_REQUESTS

# "store" requests are the ones diagnoses count; probes, robots.txt and images are not
Kind = Literal["store", "robots", "image", "probe"]


class RequestFailed(Exception):
    """A request gave no usable response after retries (transport error or redirect loop)."""

    def __init__(self, url: str, cause: BaseException | str) -> None:
        super().__init__(f"{url}: {cause}")
        self.url = url
        self.cause = cause


class StopStore(Exception):  # noqa: N818 - control flow, not an error
    """Raised inside a store to end it early with a reason (item_limit, shutdown)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HostRegistry:
    """Per-origin state shared by all stores in a run."""

    def __init__(
        self,
        *,
        concurrent_requests: int,
        throttle: ThrottleSettings,
        respect_robots_txt: bool,
    ) -> None:
        self.concurrent_requests = concurrent_requests
        self.throttle = throttle
        self.respect_robots_txt = respect_robots_txt
        self.shutdown = asyncio.Event()  # set for a graceful stop: no new requests
        self._slots: dict[str, HostSlot] = {}
        self._robots: dict[str, asyncio.Future[RobotsRules]] = {}

    def slot(self, url: str) -> HostSlot:
        key = origin(url)
        slot = self._slots.get(key)
        if slot is None:
            slot = self._slots[key] = HostSlot(self.concurrent_requests, self.throttle)
        return slot

    async def robots(self, url: str, session: StoreSession) -> RobotsRules:
        """Return robots.txt rules for the URL's origin, fetching them once per run."""
        key = origin(url)
        future = self._robots.get(key)
        if future is None:
            future = self._robots[key] = asyncio.get_running_loop().create_future()
            rules = RobotsRules(None)
            try:
                response = await session.get(f"{key}/robots.txt", kind="robots", dedupe=False)
                # Scrapy parses any body it gets back, even an error page (which has
                # no rules, so everything is allowed)
                if response is not None:
                    rules = RobotsRules(response.text)
            except (RequestFailed, FetchError) as exc:
                logger.debug("robots.txt unavailable for %s: %s", key, exc)
            except BaseException:
                future.set_result(rules)
                raise
            future.set_result(rules)
        return await asyncio.shield(future)


@dataclass
class StoreStats:
    """Counts for one store; the store's own requests exclude robots.txt and images."""

    requests: int = 0
    responses: int = 0  # store requests only, every hop
    successes: int = 0  # store 2xx responses
    by_status: Counter[int] = field(default_factory=Counter)
    robots_disallowed: int = 0
    duplicates: int = 0
    transport_errors: Counter[str] = field(default_factory=Counter)
    tls_errors: int = 0
    images_failed: int = 0
    errors: list[str] = field(default_factory=list)

    def record_response(self, kind: Kind, status: int) -> None:
        if kind != "store":
            return
        self.responses += 1
        self.by_status[status] += 1
        if 200 <= status < 300:
            self.successes += 1


Emit = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class StoreContext:
    """What a platform adapter gets for one store."""

    session: StoreSession
    url: str  # normalized store URL, scheme://host[:port]
    products: bool = True
    collections: bool = False
    images: bool = False
    items: int = 0  # items emitted so far
    hint: str | None = None  # e.g. a likely-wrong-platform note for the caller

    @property
    def netloc(self) -> str:
        return urllib.parse.urlsplit(self.url).netloc


class StoreSession:
    """One store's view of the network."""

    def __init__(
        self,
        *,
        fetcher: Fetcher,
        registry: HostRegistry,
        retry: RetryPolicy,
        identity: IdentityPool,
        request_timeout: float,
        cookies: bool = False,
    ) -> None:
        self.fetcher = fetcher
        self.registry = registry
        self.retry = retry
        self.identity = identity
        self.request_timeout = request_timeout
        self.stats = StoreStats()
        self.cookies: dict[str, str] | None = {} if cookies else None
        self.bail_deadline: float | None = None  # set by the runner until the first item
        self._seen: set[str] = set()
        self._store_slots = asyncio.Semaphore(STORE_CONCURRENCY)

    @property
    def robots_agent(self) -> str:
        return self.identity.user_agent or ROBOTS_TOKEN

    async def get(
        self,
        url: str,
        *,
        kind: Kind = "store",
        follow_redirects: bool = True,
        dedupe: bool = True,
    ) -> Response | None:
        """Fetch a URL; return None if it was skipped (duplicate or robots.txt).

        Non-2xx responses are returned once retries are exhausted. Raises
        RequestFailed if no response could be obtained or redirects loop.
        """
        if dedupe:
            key = f"GET {canonicalize_url(url)}"
            if key in self._seen:
                self.stats.duplicates += 1
                return None
            self._seen.add(key)

        for _ in range(MAX_REDIRECTS + 1):
            response = await self._fetch_with_retries(url, kind)
            if response is None:
                return None
            if not (follow_redirects and response.status in REDIRECT_STATUSES):
                return response
            location = response.location
            target = urllib.parse.urljoin(url, location) if location else None
            if not target or urllib.parse.urlsplit(target).scheme not in ("http", "https"):
                return response
            logger.debug("Redirecting (%d) to %s from %s", response.status, target, url)
            url = target
        raise RequestFailed(url, f"more than {MAX_REDIRECTS} redirects")

    async def _allowed(self, url: str, kind: Kind) -> bool:
        if kind == "robots" or not self.registry.respect_robots_txt:
            return True
        rules = await self.registry.robots(url, self)
        if rules.allowed(url, self.robots_agent):
            return True
        if kind == "store":
            self.stats.robots_disallowed += 1
        logger.debug("Forbidden by robots.txt: %s", url)
        return False

    def _retry_cap(self) -> float | None:
        if self.bail_deadline is None:
            return None
        return max(0.0, self.bail_deadline - now())

    async def _fetch_with_retries(self, url: str, kind: Kind) -> Response | None:
        if not await self._allowed(url, kind):
            return None
        slot = self.registry.slot(url)
        attempt = 0
        while True:
            if self.registry.shutdown.is_set():
                raise StopStore("shutdown")
            response: Response | None = None
            error: FetchError | None = None
            async with self._store_slots, slot.semaphore:
                await slot.wait_turn()
                self.stats.requests += 1
                try:
                    response = await self.fetcher.fetch(
                        Request(
                            method="GET",
                            url=url,
                            identity=self.identity.current,
                            timeout=self.request_timeout,
                            cookies=self.cookies or {},
                        )
                    )
                except FetchError as exc:
                    error = exc
            if response is not None:
                slot.adjust(response.latency, response.status)
                self.stats.record_response(kind, response.status)
                if self.cookies is not None:
                    self.cookies.update(response.set_cookies)
                logger.debug("Fetched (%d) %s [%s]", response.status, url, kind)
                if not self.retry.should_retry_status(response.status, attempt):
                    return response
                if response.status in self.retry.rotate_on and self.identity.rotates:
                    self.identity.rotate()
                    logger.debug(
                        "Swapped user agent after %d: %s",
                        response.status,
                        self.identity.current.user_agent,
                    )
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                reason = str(response.status)
            else:
                assert error is not None
                name = type(error).__name__
                self.stats.transport_errors[name] += 1
                if isinstance(error, FetchTLSError):
                    self.stats.tls_errors += 1
                logger.debug("Error fetching %s: %s", url, error)
                retryable = not isinstance(error, (FetchTLSError, FetchTooLarge, FetchBadEncoding))
                if not (retryable and attempt < self.retry.retries):
                    raise RequestFailed(url, error) from error
                retry_after = None
                reason = name
            delay = self.retry.delay(attempt, retry_after, self._retry_cap())
            attempt += 1
            logger.info("Retrying %s (attempt %d, %s) in %.1fs", url, attempt, reason, delay)
            if delay:
                await asyncio.sleep(delay)
