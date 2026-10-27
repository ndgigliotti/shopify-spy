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
from typing import Any, Generic, TypeVar

from shopify_spy._fetch import (
    Fetcher,
    FetchError,
    FetchInvalidURL,
    FetchTLSError,
    FetchTooLarge,
    Request,
    Response,
)
from shopify_spy._policy import (
    RATE_LIMIT_STATUSES,
    ROBOTS_TOKEN,
    HostSlot,
    IdentityPool,
    RetryPolicy,
    RobotsRules,
    ThrottleSettings,
    now,
    parse_retry_after,
)
from shopify_spy._utils import canonicalize_url, origin

logger = logging.getLogger("shopify_spy")

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 20
STORE_CONCURRENCY = 16  # per-store cap, like Scrapy's CONCURRENT_REQUESTS

T = TypeVar("T")


@dataclass(frozen=True)
class RequestKind:
    """How the session treats a request."""

    name: str
    check_robots: bool = True  # obey robots.txt for this request
    counted: bool = True  # counts toward the store's diagnosis stats
    dedupe: bool = True  # skip a URL this store already requested


STORE = RequestKind("store")
ROBOTS = RequestKind("robots", check_robots=False, counted=False, dedupe=False)
IMAGE = RequestKind("image", counted=False, dedupe=False)
PROBE = RequestKind("probe", counted=False, dedupe=False)  # e.g. a wrong-platform check


class RequestFailed(Exception):
    """A request gave no usable response after retries (transport error or redirect loop)."""

    def __init__(self, url: str, cause: BaseException | str) -> None:
        super().__init__(f"{url}: {cause}")
        self.url = url
        self.cause = cause


class StopStore(Exception):  # noqa: N818 - control flow, not an error
    """Raised by the runner inside a store to end it early (e.g. at the item limit)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ShutdownRequested(Exception):  # noqa: N818 - control flow, not an error
    """Raised instead of starting a request once a graceful shutdown was requested."""


class SingleFlight(Generic[T]):
    """Runs one fetch per key; concurrent callers for that key await its result.

    If the caller doing the fetch is cancelled, a waiting caller fetches instead of
    getting a result that was never produced.
    """

    def __init__(self) -> None:
        self._futures: dict[str, asyncio.Future[tuple[bool, T | None]]] = {}

    async def get(self, key: str, fetch: Callable[[], Awaitable[T]]) -> T:
        while (future := self._futures.get(key)) is not None:
            done, value = await asyncio.shield(future)
            if done:
                return value  # type: ignore[return-value]
        future = asyncio.get_running_loop().create_future()
        self._futures[key] = future
        try:
            value = await fetch()
        except BaseException:
            del self._futures[key]
            future.set_result((False, None))
            raise
        future.set_result((True, value))
        return value


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
        self._robots: SingleFlight[RobotsRules] = SingleFlight()

    def slot(self, url: str) -> HostSlot:
        key = origin(url)
        slot = self._slots.get(key)
        if slot is None:
            slot = self._slots[key] = HostSlot(self.concurrent_requests, self.throttle)
        return slot

    async def robots(self, url: str, session: StoreSession) -> RobotsRules:
        """Return robots.txt rules for the URL's origin, fetching them once per run."""
        key = origin(url)
        return await self._robots.get(key, lambda: _fetch_robots(key, session))


async def _fetch_robots(key: str, session: StoreSession) -> RobotsRules:
    try:
        response = await session.get(f"{key}/robots.txt", kind=ROBOTS)
    except (RequestFailed, FetchError) as exc:
        logger.debug("robots.txt unavailable for %s: %s", key, exc)
        return RobotsRules(None)
    # Scrapy parses any body it gets back, even an error page (which has no rules,
    # so everything is allowed)
    return RobotsRules(response.text if response is not None else None)


def _redirect_target(url: str, location: str | None) -> str | None:
    """The absolute http(s) URL a redirect points to, or None if it can't be followed."""
    if not location:
        return None
    try:
        target = urllib.parse.urljoin(url, location)
        scheme = urllib.parse.urlsplit(target).scheme
    except ValueError:  # e.g. "http://[::1" in Location
        return None
    return target if scheme in ("http", "https") else None


@dataclass
class StoreStats:
    """Counts for one store. Apart from ``requests``, only counted request kinds
    (the store's own requests, not robots.txt, images or probes) are included."""

    requests: int = 0  # every request attempt, any kind
    responses: int = 0  # every hop
    successes: int = 0  # 2xx responses
    by_status: Counter[int] = field(default_factory=Counter)
    robots_disallowed: int = 0
    duplicates: int = 0
    transport_errors: Counter[str] = field(default_factory=Counter)
    tls_errors: int = 0
    images_failed: int = 0


Emit = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class StoreContext:
    """What a platform adapter gets for one store.

    Adapters are ``async def run(ctx, emit) -> bool``: they emit items and return
    whether they reached the end of the store's catalog.
    """

    session: StoreSession
    url: str  # normalized store URL, scheme://host[:port]
    products: bool = True
    collections: bool = False
    images: bool = False
    items: int = 0  # items emitted so far (maintained by the runner)
    suggested_platform: str | None = None  # set when the store looks like another platform

    async def probe_platform(
        self, path: str, looks_right: Callable[[Any], bool], platform: str
    ) -> None:
        """Suggest ``platform`` if ``path`` answers 200 with JSON that ``looks_right``."""
        try:
            response = await self.session.get(f"{self.url}{path}", kind=PROBE)
        except RequestFailed:
            return
        if response is not None and response.status == 200:
            if looks_right(response.json_or_none()):
                self.suggested_platform = platform


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
    ) -> None:
        self.fetcher = fetcher
        self.registry = registry
        self.retry = retry
        self.identity = identity
        self.request_timeout = request_timeout
        self.stats = StoreStats()
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
        kind: RequestKind = STORE,
        follow_redirects: bool = True,
    ) -> Response | None:
        """Fetch a URL; return None if it was skipped (duplicate or robots.txt).

        Non-2xx responses are returned once retries are exhausted. Raises
        RequestFailed if no response could be obtained or redirects loop, and
        ShutdownRequested instead of starting a request after a graceful shutdown
        was requested.
        """
        if kind.dedupe and self._is_duplicate(url):
            return None
        for _ in range(MAX_REDIRECTS + 1):
            response = await self._fetch_with_retries(url, kind)
            if response is None:
                return None
            if not (follow_redirects and response.status in REDIRECT_STATUSES):
                return response
            target = _redirect_target(url, response.location)
            if target is None:
                return response
            logger.debug("Redirecting (%d) to %s from %s", response.status, target, url)
            # Several URLs redirecting to one page yield it once, like Scrapy's dupefilter
            if kind.dedupe and self._is_duplicate(target):
                return None
            url = target
        raise RequestFailed(url, f"more than {MAX_REDIRECTS} redirects")

    def _is_duplicate(self, url: str) -> bool:
        """Record the URL as requested; True (and counted) if it already was."""
        try:
            key = f"GET {canonicalize_url(url)}"
        except ValueError as exc:  # e.g. a malformed IPv6 host
            raise RequestFailed(url, f"invalid URL: {exc}") from exc
        if key in self._seen:
            self.stats.duplicates += 1
            return True
        self._seen.add(key)
        return False

    async def _allowed(self, url: str, kind: RequestKind) -> bool:
        if not kind.check_robots or not self.registry.respect_robots_txt:
            return True
        rules = await self.registry.robots(url, self)
        if rules.allowed(url, self.robots_agent):
            return True
        if kind.counted:
            self.stats.robots_disallowed += 1
        logger.info("Forbidden by robots.txt: %s", url)
        return False

    def _retry_cap(self) -> float | None:
        if self.bail_deadline is None:
            return None
        return max(0.0, self.bail_deadline - now())

    async def _fetch_with_retries(self, url: str, kind: RequestKind) -> Response | None:
        if not await self._allowed(url, kind):
            return None
        slot = self.registry.slot(url)
        attempt = 0
        while True:
            if self.registry.shutdown.is_set():
                raise ShutdownRequested
            response: Response | None = None
            error: FetchError | None = None
            async with self._store_slots, slot.semaphore:
                sent_at = await slot.wait_turn()
                self.stats.requests += 1
                try:
                    response = await self.fetcher.fetch(
                        Request(
                            method="GET",
                            url=url,
                            identity=self.identity.current,
                            timeout=self.request_timeout,
                        )
                    )
                except FetchError as exc:
                    error = exc
            if response is not None:
                slot.adjust(response.latency, response.status)
                if kind.counted:
                    self.stats.responses += 1
                    self.stats.by_status[response.status] += 1
                    self.stats.successes += response.ok
                logger.debug(
                    "GET %s -> %d in %.2fs (attempt %d, %s)",
                    url,
                    response.status,
                    response.latency,
                    attempt + 1,
                    kind.name,
                )
                retry_after = parse_retry_after(response.headers.get("retry-after"))
                rate_limited = response.status in RATE_LIMIT_STATUSES
                if not self.retry.should_retry_status(response.status, attempt):
                    if rate_limited:  # no retry left, but the host still has to back off
                        wait = self.retry.delay(attempt, retry_after, self._retry_cap(), True)
                        slot.pause(wait, sent_at)
                    return response
                if response.status in self.retry.rotate_on and self.identity.rotates:
                    self.identity.rotate()
                    logger.debug(
                        "Swapped user agent after %d: %s",
                        response.status,
                        self.identity.current.user_agent,
                    )
                reason = str(response.status)
            else:
                assert error is not None
                name = type(error).__name__
                if kind.counted:
                    self.stats.transport_errors[name] += 1
                    self.stats.tls_errors += isinstance(error, FetchTLSError)
                logger.debug("Error fetching %s: %s", url, error)
                retryable = not isinstance(error, (FetchTLSError, FetchTooLarge, FetchInvalidURL))
                if not (retryable and attempt < self.retry.retries):
                    raise RequestFailed(url, error) from error
                retry_after = None
                rate_limited = False
                reason = name
            delay = self.retry.delay(attempt, retry_after, self._retry_cap(), rate_limited)
            if rate_limited:
                slot.pause(delay, sent_at)
            attempt += 1
            logger.info("Retrying %s (attempt %d, %s) in %.1fs", url, attempt, reason, delay)
            if delay:
                await asyncio.sleep(delay)
