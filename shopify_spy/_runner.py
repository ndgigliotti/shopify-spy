"""Run orchestration: one task per store, merged into a single item queue.

A long-lived driver task owns every store task and puts items on a bounded queue;
both public APIs read from that queue. No TaskGroup or timeout is ever held across a
``yield`` (see .context/httpx-library-rewrite.md §4.4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import traceback
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shopify_spy import _shopify as shopify
from shopify_spy import _woocommerce as woocommerce
from shopify_spy._fetch import FetcherFactory, HttpxFetcher
from shopify_spy._images import RESULT_FIELD as IMAGES_FIELD
from shopify_spy._images import ImageDownloader
from shopify_spy._policy import IdentityPool, ThrottleSettings, retry_policy_for
from shopify_spy._results import StoreResult, diagnose
from shopify_spy._session import (
    Emit,
    HostRegistry,
    ShutdownRequested,
    StopStore,
    StoreContext,
    StoreSession,
    StoreStats,
)

logger = logging.getLogger("shopify_spy")

QUEUE_SIZE = 100
IMAGE_ITEMS_PER_STORE = 8  # items waiting on image downloads at once, per store
SHUTDOWN_GRACE = 10.0  # seconds in-flight requests get after a graceful shutdown

ADAPTERS: dict[str, Callable[[StoreContext, Emit], Awaitable[bool]]] = {
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
    """One store's progress; ``ctx`` (once the store starts) holds its live state."""

    input_url: str
    url: str
    ctx: StoreContext | None = None
    finish_reason: str | None = None
    errored: bool = False
    truncated: bool = False  # the adapter stopped short of the catalog after items
    started: float | None = None
    ended: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def items(self) -> int:
        return self.ctx.items if self.ctx else 0

    def result(self, platform: str) -> StoreResult:
        stats = self.ctx.session.stats if self.ctx else StoreStats()
        reason = self.finish_reason or "closed"
        duration = 0.0
        if self.started is not None:
            duration = (self.ended or time.monotonic()) - self.started
        return StoreResult(
            input_url=self.input_url,
            url=self.url,
            store=urllib.parse.urlsplit(self.url).netloc,
            platform=platform,
            items=self.items,
            status=diagnose(
                items=self.items,
                errored=self.errored,
                truncated=self.truncated,
                finish_reason=reason,
                stats=stats,
            ),
            complete=reason == "finished" and not self.errored and not self.truncated,
            finish_reason=reason,
            suggested_platform=self.ctx.suggested_platform if self.ctx else None,
            responses_by_status=dict(stats.by_status),
            robots_disallowed=stats.robots_disallowed,
            images_failed=stats.images_failed,
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
        self._driver: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._driver = asyncio.get_running_loop().create_task(self._drive())

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
        finally:
            # Nothing is delivered after a close; later reads just end
            while not self.queue.empty():
                self.queue.get_nowait()
            self.queue.put_nowait(DONE)

    def _stopped(self) -> str:
        """Finish reason for a store that didn't end on its own."""
        return "shutdown" if self.shutdown.is_set() else "closed"

    async def _drive(self) -> None:
        watcher: asyncio.Task[None] | None = None
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
                    tasks = [
                        tg.create_task(self._store(state, fetcher, registry, run_slots))
                        for state in self.states
                    ]
                    watcher = asyncio.create_task(self._enforce_shutdown_grace(tasks))
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # a bug in the runner itself
            await self.queue.put(_Failure(_unwrap(exc)))
            return
        finally:
            if watcher is not None:
                watcher.cancel()
            for state in self.states:
                if state.finish_reason is None:
                    state.finish_reason = self._stopped()
        await self.queue.put(DONE)

    async def _enforce_shutdown_grace(self, tasks: list[asyncio.Task[None]]) -> None:
        """After a graceful shutdown request, cancel whatever is still running."""
        await self.shutdown.wait()
        await asyncio.sleep(SHUTDOWN_GRACE)
        logger.warning("Shutdown grace period over; cancelling remaining requests")
        for task in tasks:
            task.cancel()

    async def _store(
        self,
        state: StoreState,
        fetcher: Any,
        registry: HostRegistry,
        run_slots: asyncio.Semaphore | None,
    ) -> None:
        async with run_slots or contextlib.nullcontext():
            await self._scrape_store(state, fetcher, registry)

    async def _scrape_store(self, state: StoreState, fetcher: Any, registry: HostRegistry) -> None:
        config = self.config
        state.started = time.monotonic()
        session = StoreSession(
            fetcher=fetcher,
            registry=registry,
            retry=retry_policy_for(fetcher.capabilities, config.retries, config.backoff),
            identity=IdentityPool(user_agent=config.user_agent),
            request_timeout=config.request_timeout,
        )
        ctx = state.ctx = StoreContext(
            session=session,
            url=state.url,
            products=config.products,
            collections=config.collections,
            images=config.images,
        )
        downloader = (
            ImageDownloader(session, config.images_dir)
            if config.images and config.images_dir is not None
            else None
        )
        # Each emitted item is handed to its own delivery task (image downloads, then
        # the queue). A slot is taken *before* the item is counted, so cancelling an
        # adapter task (item limit, shutdown) can never orphan a counted item. Without
        # images one slot is enough: it still gives queue backpressure.
        delivery_slots = asyncio.Semaphore(IMAGE_ITEMS_PER_STORE if downloader else 1)
        deliveries: set[asyncio.Task[None]] = set()
        loop = asyncio.get_running_loop()
        first_item = asyncio.Event()
        limit = config.max_items_per_store

        async def deliver(item: dict[str, Any]) -> None:
            try:
                if downloader is not None:
                    try:
                        item = await downloader.process(item)
                    except Exception as exc:  # never lose the item over its images
                        logger.error("Image processing failed for %s: %r", state.url, exc)
                        state.errors.append(repr(exc))
                        item.setdefault(IMAGES_FIELD, [])
                await self.queue.put(item)
            finally:
                delivery_slots.release()

        async def emit(item: dict[str, Any]) -> None:
            await delivery_slots.acquire()
            if limit is not None and ctx.items >= limit:
                delivery_slots.release()
                raise StopStore("item_limit")
            # No awaits from here on: counting and handing off happen together
            ctx.items += 1
            if ctx.items == 1:
                first_item.set()
                session.bail_deadline = None
            task = loop.create_task(deliver(item))
            deliveries.add(task)
            task.add_done_callback(deliveries.discard)
            if limit is not None and ctx.items >= limit:
                raise StopStore("item_limit")

        adapter = ADAPTERS[config.platform]
        logger.info("Store %s started (%s)", state.url, config.platform)
        # The adapter runs as its own task so the bail watchdog below can cancel it
        # without racing an item that arrives at the deadline
        adapter_task = loop.create_task(adapter(ctx, emit))
        try:
            if config.bail is not None and not await self._first_item_in_time(
                adapter_task, first_item, session, config.bail
            ):
                logger.warning("No items scraped from %s in %ss, aborting", state.url, config.bail)
                state.finish_reason = "bail"
            else:
                complete = await adapter_task
                state.truncated = not complete and ctx.items > 0
                state.finish_reason = "shutdown" if self.shutdown.is_set() else "finished"
        except* StopStore as group:
            state.finish_reason = group.exceptions[0].reason  # type: ignore[attr-defined]
        except* ShutdownRequested:
            state.finish_reason = "shutdown"
        except* Exception as group:
            state.errored = True
            state.finish_reason = "error"
            for exc in group.exceptions:
                logger.error("Store %s failed: %r", state.url, exc)
                state.errors.append("".join(traceback.format_exception(exc)))
        finally:
            if not adapter_task.done():
                adapter_task.cancel()
                await asyncio.gather(adapter_task, return_exceptions=True)
            if deliveries:
                if state.finish_reason is None:  # being cancelled: the run was closed
                    for t in deliveries:
                        t.cancel()
                await asyncio.gather(*deliveries, return_exceptions=True)
            state.ended = time.monotonic()
            if state.finish_reason is None:
                state.finish_reason = self._stopped()
            logger.info(
                "Store %s finished: %d items (%s)", state.url, ctx.items, state.finish_reason
            )

    @staticmethod
    async def _first_item_in_time(
        adapter_task: asyncio.Task[bool],
        first_item: asyncio.Event,
        session: StoreSession,
        bail: float,
    ) -> bool:
        """Wait until the store's first item (or its end); False if bail ran out first.

        On a bail the adapter task is cancelled and awaited.
        """
        session.bail_deadline = asyncio.get_running_loop().time() + bail
        waiter = asyncio.ensure_future(first_item.wait())
        try:
            await asyncio.wait({adapter_task, waiter}, timeout=bail, return_when="FIRST_COMPLETED")
        finally:
            waiter.cancel()
        if adapter_task.done() or first_item.is_set():
            return True
        adapter_task.cancel()
        await asyncio.gather(adapter_task, return_exceptions=True)
        return False

    def results(self) -> list[StoreResult]:
        return [s.result(self.config.platform) for s in self.states]


def _unwrap(exc: BaseException) -> BaseException:
    """Return the first leaf exception of a (possibly nested) exception group."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc
