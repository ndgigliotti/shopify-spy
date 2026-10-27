"""Run orchestration: one task per store, merged into a single item queue.

A long-lived driver task owns every store task and puts items on a bounded queue;
both public APIs read from that queue. No TaskGroup or timeout is ever held across a
``yield`` (see .context/httpx-library-rewrite.md §4.4).
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shopify_spy import shopify, woocommerce
from shopify_spy.fetch import FetcherFactory, HttpxFetcher
from shopify_spy.images import ImageDownloader
from shopify_spy.policy import IdentityPool, ThrottleSettings, retry_policy_for
from shopify_spy.results import StoreResult, diagnose
from shopify_spy.session import HostRegistry, StopStore, StoreContext, StoreSession

logger = logging.getLogger("shopify_spy")

QUEUE_SIZE = 100
IMAGE_ITEMS_PER_STORE = 8  # items waiting on image downloads at once, per store
SHUTDOWN_GRACE = 10.0  # seconds in-flight requests get after a graceful shutdown

ADAPTERS: dict[str, Callable[[StoreContext, Any], Awaitable[None]]] = {
    "shopify": shopify.run,
    "woocommerce": woocommerce.run,
}


@dataclass
class RunConfig:
    """Validated run options (see api.ScrapeOptions for the public names)."""

    platform: str = "shopify"
    products: bool = True
    collections: bool = False
    images: bool = False
    images_dir: Path | None = None
    max_items_per_store: int | None = None
    bail: float | None = 30.0
    concurrent_requests: int = 16
    request_timeout: float = 180.0
    retries: int = 2
    user_agent: str | None = None
    respect_robots_txt: bool = True
    verify_tls: bool = True
    max_concurrent_stores: int | None = 16
    throttle: ThrottleSettings = field(default_factory=ThrottleSettings)
    backoff: bool = True
    fetcher_factory: FetcherFactory | None = None


@dataclass
class StoreState:
    input_url: str
    url: str
    items: int = 0
    finish_reason: str | None = None
    errored: bool = False
    truncated: bool = False
    started: float | None = None
    ended: float | None = None
    session: StoreSession | None = None
    ctx: StoreContext | None = None
    errors: list[str] = field(default_factory=list)

    def result(self, platform: str) -> StoreResult:
        stats = self.session.stats if self.session else None
        reason = self.finish_reason or "closed"
        status = diagnose(
            items=self.items,
            errored=self.errored,
            truncated=self.truncated,
            finish_reason=reason,
            successes=stats.successes if stats else 0,
            responses=stats.responses if stats else 0,
            by_status=dict(stats.by_status) if stats else {},
            robots_disallowed=stats.robots_disallowed if stats else 0,
            tls_errors=stats.tls_errors if stats else 0,
        )
        duration = 0.0
        if self.started is not None:
            duration = (self.ended or time.monotonic()) - self.started
        return StoreResult(
            input_url=self.input_url,
            url=self.url,
            store=urllib.parse.urlsplit(self.url).netloc,
            platform=platform,
            items=self.items,
            status=status,
            complete=reason == "finished" and not self.errored,
            finish_reason=reason,
            hint=self.ctx.hint if self.ctx else None,
            responses_by_status=dict(stats.by_status) if stats else {},
            robots_disallowed=stats.robots_disallowed if stats else 0,
            images_failed=stats.images_failed if stats else 0,
            errors=list(self.errors),
            duration=duration,
        )


DONE = object()


class _Failure:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc


class Runner:
    """Scrapes stores and feeds items into ``queue``; ``DONE`` marks the end."""

    def __init__(self, stores: list[tuple[str, str]], config: RunConfig) -> None:
        self.config = config
        self.states = [StoreState(input_url=i, url=u) for i, u in stores]
        self.queue: asyncio.Queue[Any] = asyncio.Queue(QUEUE_SIZE)
        self.shutdown = asyncio.Event()
        self.started = time.monotonic()
        self._driver: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._driver = asyncio.get_running_loop().create_task(self._drive())

    @property
    def done(self) -> bool:
        return self._driver is not None and self._driver.done()

    async def next(self) -> dict[str, Any]:
        """Return the next item; raise StopAsyncIteration at the end."""
        item = await self.queue.get()
        if item is DONE:
            self.queue.put_nowait(DONE)  # keep later calls ending too
            raise StopAsyncIteration
        if isinstance(item, _Failure):
            raise item.exc
        return item

    def request_shutdown(self) -> None:
        if not self.shutdown.is_set():
            logger.info("Shutdown requested; finishing in-flight requests")
            self.shutdown.set()

    async def close(self) -> None:
        """Cancel the run now; unfinished stores end as "closed"."""
        if self._driver is None or self._driver.done():
            return
        self._driver.cancel()
        try:
            await self._driver
        except asyncio.CancelledError:
            if asyncio.current_task() and asyncio.current_task().cancelling():
                raise

    async def _drive(self) -> None:
        try:
            factory = self.config.fetcher_factory or (
                lambda: HttpxFetcher(verify=self.config.verify_tls)
            )
            async with factory() as fetcher:
                registry = HostRegistry(
                    concurrent_requests=self.config.concurrent_requests,
                    throttle=self.config.throttle,
                    respect_robots_txt=self.config.respect_robots_txt,
                )
                registry.shutdown = self.shutdown
                limit = self.config.max_concurrent_stores
                run_slots = asyncio.Semaphore(limit) if limit else None
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._watch_shutdown())
                    for state in self.states:
                        tg.create_task(self._store(state, fetcher, registry, run_slots))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # a bug in the runner itself
            exc = _unwrap(exc)
            await self.queue.put(_Failure(exc))
            return
        finally:
            for state in self.states:
                if state.finish_reason is None:
                    state.finish_reason = "shutdown" if self.shutdown.is_set() else "closed"
        await self.queue.put(DONE)

    async def _watch_shutdown(self) -> None:
        """After a graceful shutdown request, cancel whatever is still running."""
        if all(s.finish_reason for s in self.states):
            return
        waiter = asyncio.ensure_future(self.shutdown.wait())
        try:
            while not all(s.finish_reason for s in self.states):
                done, _ = await asyncio.wait({waiter}, timeout=0.2)
                if done:
                    break
            else:
                return
        finally:
            waiter.cancel()
        await asyncio.sleep(SHUTDOWN_GRACE)
        if not all(s.finish_reason for s in self.states):
            logger.warning("Shutdown grace period over; cancelling remaining requests")
            for task in asyncio.all_tasks():
                if getattr(task, "_shopify_spy_store", False):
                    task.cancel()

    async def _store(
        self,
        state: StoreState,
        fetcher: Any,
        registry: HostRegistry,
        run_slots: asyncio.Semaphore | None,
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            task._shopify_spy_store = True  # type: ignore[attr-defined]
        if run_slots is not None:
            async with run_slots:
                await self._scrape_store(state, fetcher, registry)
        else:
            await self._scrape_store(state, fetcher, registry)

    async def _scrape_store(self, state: StoreState, fetcher: Any, registry: HostRegistry) -> None:
        config = self.config
        state.started = time.monotonic()
        session = StoreSession(
            fetcher=fetcher,
            registry=registry,
            retry=retry_policy_for(
                fetcher.capabilities, config.retries, config.backoff, config.throttle.max_delay
            ),
            identity=IdentityPool(user_agent=config.user_agent),
            request_timeout=config.request_timeout,
        )
        state.session = session
        ctx = StoreContext(
            session=session,
            url=state.url,
            products=config.products,
            collections=config.collections,
            images=config.images,
        )
        state.ctx = ctx
        downloader = (
            ImageDownloader(session, config.images_dir)
            if config.images and config.images_dir is not None
            else None
        )
        image_slots = asyncio.Semaphore(IMAGE_ITEMS_PER_STORE)
        image_tasks: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        bail = config.bail if config.bail else None
        timeout_cm = asyncio.timeout(None)
        limit = config.max_items_per_store

        async def deliver(item: dict[str, Any]) -> None:
            try:
                if downloader is not None:
                    item = await downloader.process(item)
                await self.queue.put(item)
            finally:
                image_slots.release()

        async def emit(item: dict[str, Any]) -> None:
            if limit is not None and state.items >= limit:
                raise StopStore("item_limit")
            state.items += 1
            ctx.items = state.items
            if state.items == 1:
                timeout_cm.reschedule(None)
                session.bail_deadline = None
            if downloader is not None:
                await image_slots.acquire()
                t = loop.create_task(deliver(item))
                image_tasks.add(t)
                t.add_done_callback(image_tasks.discard)
            else:
                await self.queue.put(item)
            if limit is not None and state.items >= limit:
                raise StopStore("item_limit")

        adapter = ADAPTERS[config.platform]
        try:
            try:
                async with timeout_cm:
                    if bail is not None:
                        timeout_cm.reschedule(loop.time() + bail)
                        session.bail_deadline = loop.time() + bail
                    await adapter(ctx, emit)
                state.finish_reason = "shutdown" if self.shutdown.is_set() else "finished"
            except TimeoutError:
                if not timeout_cm.expired():
                    raise
                logger.warning("No items scraped from %s in %ss, aborting", state.url, bail)
                state.finish_reason = "bail"
        except* StopStore as group:
            state.finish_reason = group.exceptions[0].reason  # type: ignore[attr-defined]
        except* Exception as group:
            state.errored = True
            state.finish_reason = "error"
            for exc in group.exceptions:
                logger.error("Store %s failed: %r", state.url, exc)
                state.errors.append("".join(traceback.format_exception(exc)))
        finally:
            if image_tasks:
                if state.finish_reason in ("closed", None):
                    for t in image_tasks:
                        t.cancel()
                await asyncio.gather(*image_tasks, return_exceptions=True)
            state.ended = time.monotonic()
            if state.finish_reason is None:
                state.finish_reason = "shutdown" if self.shutdown.is_set() else "closed"
            logger.info(
                "Store %s finished: %d items (%s)", state.url, state.items, state.finish_reason
            )

    def results(self) -> list[StoreResult]:
        return [s.result(self.config.platform) for s in self.states]


def _unwrap(exc: BaseException) -> BaseException:
    """Return the first leaf exception of a (possibly nested) exception group."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc
