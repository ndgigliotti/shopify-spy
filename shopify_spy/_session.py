"""The request pipeline: destination check, robots.txt, deduplication, slots,
throttling, retries, redirects.

``HostRegistry`` holds per-host state shared by every store in a run (robots.txt
rules, resolved addresses, concurrency and throttle slots), keyed by origin
(scheme://host:port).
``StoreSession`` holds one store's state: identity, cookies, seen URLs, stats.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import urllib.parse
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from shopify_spy._address import (
    IPAddress,
    Resolver,
    is_local_name,
    is_private_host,
    literal_ip,
    non_public_reason,
)
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
from shopify_spy._utils import canonicalize_url, hostname, origin

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
    rotate: bool = True  # a 403 or 429 may swap the store's user agent; without it a 403 is final
    # A 429 counts toward StoreStats.rate_limits: its pause holds up the store's data
    # (images come after the data they belong to)
    rate_limits: bool = True
    # A slot shared by every URL of this kind, instead of one per origin
    slot: str | None = None
    # Spaced by the throttle's image_delay instead of min_delay
    image: bool = False


STORE = RequestKind("store")
ROBOTS = RequestKind("robots", check_robots=False, counted=False, dedupe=False)
IMAGE = RequestKind("image", counted=False, dedupe=False, rate_limits=False, image=True)
PROBE = RequestKind("probe", counted=False, dedupe=False)  # e.g. a wrong-platform check
# A fallback source, e.g. WooCommerce product pages: if it finds nothing, the store's
# diagnosis still explains why the primary source failed
FALLBACK = RequestKind("fallback", counted=False)
# A headless store's products read from its *.myshopify.com domain (issue #92). Those
# hosts belong to Shopify, not the store: one run-wide slot keeps a run over many
# stores from bursting at them, and a refusal there doesn't change the store's agent
MYSHOPIFY = RequestKind("myshopify", counted=False, rotate=False, slot="*.myshopify.com")


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
        resolver: Resolver | None = None,
    ) -> None:
        self.concurrent_requests = concurrent_requests
        self.throttle = throttle
        self.respect_robots_txt = respect_robots_txt
        self.shutdown = asyncio.Event()  # set for a graceful stop: no new requests
        self._slots: dict[str, HostSlot] = {}
        self._robots: SingleFlight[RobotsRules] = SingleFlight()
        self._crawl_delays: dict[str, float] = {}  # applied Crawl-delay by origin
        # None: hostnames aren't looked up (a proxy or custom transport resolves them)
        self.resolver = resolver
        self._resolved: SingleFlight[list[IPAddress]] = SingleFlight()

    def slot(self, url: str, key: str | None = None) -> HostSlot:
        """The URL's origin slot, or the shared slot ``key``, which sends one request
        at a time."""
        name = origin(url) if key is None else key
        slot = self._slots.get(name)
        if slot is None:
            concurrency = self.concurrent_requests if key is None else 1
            slot = self._slots[name] = HostSlot(concurrency, self.throttle)
        return slot

    async def robots(
        self, url: str, session: StoreSession, kind: RequestKind = STORE
    ) -> RobotsRules:
        """Return robots.txt rules for the URL's origin, fetching them once per run.

        The fetch takes the slot and rotation of the ``kind`` that asked. Its
        Crawl-delay goes to the slot of every kind that asks, since an origin can be
        reached through both its own slot and a shared one.
        """
        key = origin(url)
        robots_kind = dataclasses.replace(ROBOTS, rotate=kind.rotate, slot=kind.slot)

        async def fetch() -> RobotsRules:
            rules = await _fetch_robots(key, session, robots_kind)
            self._note_crawl_delay(key, rules)
            return rules

        rules = await self._robots.get(key, fetch)
        if (delay := self._crawl_delays.get(key)) is not None:
            self.slot(key, kind.slot).set_crawl_delay(delay)
        return rules

    def _note_crawl_delay(self, key: str, rules: RobotsRules) -> None:
        """Record the origin's robots.txt Crawl-delay, capped at the throttle's
        max_delay; called once per origin."""
        requested = rules.crawl_delay()
        if requested is None:
            return
        applied = min(requested, self.throttle.max_delay)
        if applied < requested:
            logger.warning(
                "robots.txt Crawl-delay of %gs for %s is over the %gs maximum; using %gs",
                requested,
                key,
                self.throttle.max_delay,
                applied,
            )
        else:
            logger.info("Using robots.txt Crawl-delay of %gs for %s", applied, key)
        self._crawl_delays[key] = applied

    async def resolved(self, url: str, timeout: float) -> list[IPAddress]:
        """The addresses the URL's host resolves to, looked up once per origin per run;
        [] without a resolver, for a literal IP or local name, or if the lookup fails."""
        host = hostname(url)
        resolver = self.resolver
        if resolver is None or not host or literal_ip(host) is not None or is_local_name(host):
            return []

        async def lookup() -> list[IPAddress]:
            # A stalled lookup counts as a failed one, so the transport (whose own
            # lookup runs under request_timeout) reports what happens next
            try:
                async with asyncio.timeout(timeout):
                    return await resolver(url)
            except TimeoutError:
                logger.debug("Address lookup timed out for %s", host)
                return []

        return await self._resolved.get(origin(url), lookup)


async def _fetch_robots(key: str, session: StoreSession, kind: RequestKind) -> RobotsRules:
    try:
        response = await session.get(f"{key}/robots.txt", kind=kind)
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
    # Requests that ended on a 429, of any kind but images
    rate_limits: int = 0
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
    item_limit: int | None = None  # the runner stops the store at this many items
    suggested_platform: str | None = None  # set when the store looks like another platform
    products_from: str | None = None  # the host bulk products came from, if not the store's

    @property
    def items_left(self) -> int | None:
        """Items still to emit before the item limit; None without a limit."""
        return None if self.item_limit is None else self.item_limit - self.items

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
        store_url: str,
    ) -> None:
        self.fetcher = fetcher
        self.registry = registry
        self.retry = retry
        self.identity = identity
        self.request_timeout = request_timeout
        self.stats = StoreStats()
        self.bail_deadline: float | None = None  # set by the runner until the first item
        self._excused_ends: list[float] = []  # ends of Retry-After waits excused from bail
        self._seen: set[str] = set()
        self._store_slots = asyncio.Semaphore(STORE_CONCURRENCY)
        self.store_url = store_url
        # Computed on the first request, inside the store's task (so under --bail)
        self._store_is_private: bool | None = None

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

    async def _store_private(self) -> bool:
        """Whether the store's own URL is non-public, which exempts its requests."""
        if self._store_is_private is None:
            url = self.store_url
            resolved = await self.registry.resolved(url, self.request_timeout)
            self._store_is_private = is_private_host(hostname(url), resolved)
        return self._store_is_private

    async def _check_destination(self, url: str) -> None:
        """Raise RequestFailed for a non-public destination, unless the store is one."""
        if await self._store_private():
            return
        resolved = await self.registry.resolved(url, self.request_timeout)
        reason = non_public_reason(hostname(url), resolved)
        if reason is not None:
            logger.warning("Refused request to %s: %s", url, reason)
            raise RequestFailed(url, f"refused: {reason}")

    async def _allowed(self, url: str, kind: RequestKind) -> bool:
        if not kind.check_robots or not self.registry.respect_robots_txt:
            return True
        rules = await self.registry.robots(url, self, kind)
        if rules.allowed(url, self.robots_agent):
            return True
        if kind.counted:
            self.stats.robots_disallowed += 1
        logger.info("Forbidden by robots.txt: %s", url)
        return False

    def bail_time_left(self) -> float | None:
        """Seconds until --bail ends a store with no items yet; None once it has one."""
        if self.bail_deadline is None:
            return None
        return max(0.0, self.bail_deadline - now())

    def _excuse_from_bail(self, seconds: float) -> float | None:
        """Move the bail deadline past a wait the server asked for with Retry-After;
        return when the wait ends, or None without a deadline.

        Overlapping waits of concurrent requests count once, by their union.
        """
        if self.bail_deadline is None:
            return None
        start = now()
        end = start + seconds
        self._excused_ends = [e for e in self._excused_ends if e > start]
        covered = max(self._excused_ends, default=start)
        self.bail_deadline += max(0.0, end - covered)
        self._excused_ends.append(end)
        return end

    def _unexcuse_from_bail(self, end: float) -> None:
        """Take back the unspent part of an excused wait that was cut short (say, by
        the WooCommerce variation budget), unless another wait still covers it."""
        if end in self._excused_ends:
            self._excused_ends.remove(end)
        if self.bail_deadline is None:
            return
        cut = now()
        covered = max([cut, *self._excused_ends])
        self.bail_deadline -= max(0.0, end - covered)

    async def _sleep_before_retry(self, delay: float, excused_until: float | None) -> None:
        if not delay:
            return
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            if excused_until is not None:
                self._unexcuse_from_bail(excused_until)
            raise

    @staticmethod
    def _bail_excused(retry_after: float | None, rate_limited: bool) -> bool:
        """Whether a retry wait is excused from --bail: a server's Retry-After on a
        rate limit."""
        return rate_limited and retry_after is not None

    def _retry_delay(
        self, attempt: int, retry_after: float | None, rate_limited: bool
    ) -> tuple[float, float | None]:
        """The wait before a retry, and when its bail excusal ends if it has one; a
        server's Retry-After isn't cut short by --bail."""
        excused = self._bail_excused(retry_after, rate_limited)
        cap = None if excused else self.bail_time_left()
        delay = self.retry.delay(attempt, retry_after, cap, rate_limited)
        return delay, self._excuse_from_bail(delay) if excused else None

    def _ends_request(
        self,
        url: str,
        slot: HostSlot,
        attempt: int,
        retry_after: float | None,
        rate_limited: bool,
        will_retry: bool,
    ) -> bool:
        """Whether a response ends the request, backing off from it if so.

        A Retry-After longer than we'd wait isn't retried early: a rate limit pauses
        the host for all of it, without excusing the pause from --bail.
        """
        cap = None if self._bail_excused(retry_after, rate_limited) else self.bail_time_left()
        if not self.retry.retry_after_too_long(retry_after, cap):
            if will_retry:
                return False
            if rate_limited:  # no retry left, but the host still has to back off
                slot.pause(self._retry_delay(attempt, retry_after, True)[0])
            return True
        assert retry_after is not None
        if rate_limited:
            slot.pause(retry_after)
            logger.warning(
                "Pausing requests to %s for %.0fs on Retry-After; not retrying %s",
                origin(url),
                retry_after,
                url,
            )
        elif will_retry:
            logger.info(
                "Not retrying %s: Retry-After %.0fs is longer than the %.0fs wait allowed",
                url,
                retry_after,
                self.retry.limit(cap),
            )
        return True

    async def _cleared(self, url: str, kind: RequestKind) -> bool:
        """Whether the request may be sent; False if robots.txt disallows it."""
        # Before the address lookup, which can wait up to request_timeout
        if self.registry.shutdown.is_set():
            raise ShutdownRequested
        # Before robots.txt, so a refused host isn't sent that request either
        await self._check_destination(url)
        return await self._allowed(url, kind)

    async def _fetch_with_retries(self, url: str, kind: RequestKind) -> Response | None:
        if not await self._cleared(url, kind):
            return None
        slot = self.registry.slot(url, kind.slot)
        # Whether a 429 is this request's last word: retries used up, or a shutdown (or
        # --bail, for a wait without Retry-After) cut its wait short
        limited = False
        try:
            attempt = 0
            while True:
                if self.registry.shutdown.is_set():
                    raise ShutdownRequested
                response: Response | None = None
                error: FetchError | None = None
                async with self._store_slots, slot.semaphore:
                    sent_at = await slot.wait_turn(kind.image)
                    self.stats.requests += 1
                    sent = self.identity.current
                    try:
                        response = await self.fetcher.fetch(
                            Request(
                                method="GET",
                                url=url,
                                identity=sent,
                                timeout=self.request_timeout,
                            )
                        )
                    except FetchError as exc:
                        error = exc
                if response is not None:
                    slot.adjust(response.latency, response.status, sent_at)
                    if kind.counted:
                        self.stats.responses += 1
                        self.stats.by_status[response.status] += 1
                        self.stats.successes += response.ok
                    limited = response.status == 429
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
                    # Whatever the transport rotates on, a 403 to such a kind is final
                    refused = not kind.rotate and response.status == 403
                    will_retry = not refused and self.retry.should_retry_status(
                        response.status, attempt
                    )
                    if self._ends_request(
                        url, slot, attempt, retry_after, rate_limited, will_retry
                    ):
                        return response
                    # A concurrent refusal of the same agent may have swapped it already
                    if (
                        kind.rotate
                        and response.status in self.retry.rotate_on
                        and self.identity.rotates
                        and self.identity.current.user_agent == sent.user_agent
                    ):
                        self.identity.rotate()
                        logger.debug(
                            "Swapped user agent after %d: %s",
                            response.status,
                            self.identity.current.user_agent,
                        )
                    reason = str(response.status)
                else:
                    assert error is not None
                    limited = False
                    name = type(error).__name__
                    if kind.counted:
                        self.stats.transport_errors[name] += 1
                        self.stats.tls_errors += isinstance(error, FetchTLSError)
                    logger.debug("Error fetching %s: %s", url, error)
                    retryable = not isinstance(
                        error, (FetchTLSError, FetchTooLarge, FetchInvalidURL)
                    )
                    if not (retryable and attempt < self.retry.retries):
                        raise RequestFailed(url, error) from error
                    retry_after = None
                    rate_limited = False
                    reason = name
                delay, excused_until = self._retry_delay(attempt, retry_after, rate_limited)
                if rate_limited:
                    slot.pause(delay)
                attempt += 1
                logger.info("Retrying %s (attempt %d, %s) in %.1fs", url, attempt, reason, delay)
                await self._sleep_before_retry(delay, excused_until)
        finally:
            self.stats.rate_limits += limited and kind.rate_limits
